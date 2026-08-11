"""Retention, the trash, and the lock.

This is the only code in the project that deletes something nobody asked it
to delete, so the properties worth pinning are the refusals: that a locked
report is unreachable by every automatic path, that "retire the fourth
report" never means "destroy it", and that the filters actually filter.

That last one is not paranoia. `ReviewItem.locked` is a column object, so
`.where(not ReviewItem.locked)` evaluates the object's truthiness in Python,
produces `False`, and compiles to a clause that matches everything — a purge
that quietly ignores every lock. It is a one-character difference from
correct and produces no error, so it is asserted against the compiled SQL
rather than trusted.
"""
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy.dialects import postgresql

from app.database.models import ReviewItem
from app.reports.retention import (
    KEEP_LATEST,
    TRASH_TTL_DAYS,
    active,
    apply_retention,
    empty_trash,
    expires_at,
    purge_expired,
    trashed,
)

REPORT = "site_report"
NOW = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)


def _sql(statement) -> str:
    return str(statement.compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
    ))


def _item(days_old: int, *, locked: bool = False, trashed_days: int | None = None) -> ReviewItem:
    return ReviewItem(
        id=f"r{days_old}", agent="autopilot", action_type=REPORT, site_id="s1",
        payload={}, status="pending", locked=locked,
        created_at=NOW - timedelta(days=days_old),
        trashed_at=None if trashed_days is None else NOW - timedelta(days=trashed_days),
    )


class _Result:
    def __init__(self, rows: list[ReviewItem]) -> None:
        self._rows = rows
        self.rowcount = len(rows)

    def scalars(self):
        return self

    def all(self) -> list[ReviewItem]:
        return self._rows


class _FakeDb:
    """Captures statements and hands back a fixed row set."""

    def __init__(self, rows: list[ReviewItem] | None = None) -> None:
        self.rows = rows or []
        self.statements: list[str] = []
        self.flushed = False

    async def execute(self, statement):
        self.statements.append(_sql(statement))
        return _Result(self.rows)

    async def flush(self) -> None:
        self.flushed = True


class TestSelectionSql:
    def test_active_excludes_the_trash(self) -> None:
        sql = _sql(active(REPORT, "s1"))
        assert "trashed_at IS NULL" in sql
        assert "site_id = 's1'" in sql

    def test_trashed_selects_only_the_trash(self) -> None:
        assert "trashed_at IS NOT NULL" in _sql(trashed(REPORT))

    def test_a_scope_without_a_site_covers_every_site(self) -> None:
        # Used by the nightly purge, which is not per-site. Checked against
        # the WHERE clause — site_id appears in every SELECT column list.
        assert "site_id =" not in _sql(trashed(REPORT))


class TestLockIsHonouredInSql:
    """Each of these compiles to a clause that must actually constrain rows.

    A truthiness bug here does not raise; it deletes locked reports.
    """

    @pytest.mark.asyncio
    async def test_retention_skips_locked_reports(self) -> None:
        db = _FakeDb()
        await apply_retention(db, REPORT, "s1")  # type: ignore[arg-type]
        assert "locked = false" in db.statements[0]

    @pytest.mark.asyncio
    async def test_the_purge_cannot_reach_a_locked_report(self) -> None:
        db = _FakeDb()
        await purge_expired(db, REPORT)  # type: ignore[arg-type]
        assert "locked = false" in db.statements[0]
        assert "trashed_at IS NOT NULL" in db.statements[0]

    @pytest.mark.asyncio
    async def test_emptying_the_trash_cannot_reach_a_locked_report(self) -> None:
        db = _FakeDb()
        await empty_trash(db, REPORT)  # type: ignore[arg-type]
        assert "locked = false" in db.statements[0]

    @pytest.mark.asyncio
    async def test_the_purge_only_takes_items_past_the_window(self) -> None:
        db = _FakeDb()
        await purge_expired(db, REPORT, ttl_days=30)  # type: ignore[arg-type]
        assert "trashed_at <" in db.statements[0]

    @pytest.mark.asyncio
    async def test_emptying_one_site_leaves_the_others_alone(self) -> None:
        db = _FakeDb()
        await empty_trash(db, REPORT, "s1")  # type: ignore[arg-type]
        assert "site_id = 's1'" in db.statements[0]


class TestRetentionKeepsAndRetires:
    @pytest.mark.asyncio
    async def test_the_newest_survive_and_the_rest_are_trashed_not_deleted(self) -> None:
        # Trashed, not deleted: a report may already have been sent to
        # someone, and exceeding a count is not grounds to destroy it.
        rows = [_item(d) for d in (0, 1, 2, 3, 4)]   # newest first
        db = _FakeDb(rows)
        moved = await apply_retention(db, REPORT, "s1", keep=3)  # type: ignore[arg-type]

        assert moved == ["r3", "r4"]
        assert [r.trashed_at for r in rows[:3]] == [None, None, None]
        assert all(r.trashed_at is not None for r in rows[3:])
        assert db.flushed

    @pytest.mark.asyncio
    async def test_nothing_moves_when_under_the_limit(self) -> None:
        db = _FakeDb([_item(0), _item(1)])
        assert await apply_retention(db, REPORT, "s1", keep=3) == []  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_it_reports_what_it_moved(self) -> None:
        # Returned so the caller can say how many, rather than leaving a
        # silent deletion to be discovered later.
        db = _FakeDb([_item(d) for d in range(6)])
        assert len(await apply_retention(db, REPORT, "s1", keep=3)) == 3  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_retention_is_ordered_newest_first(self) -> None:
        # The slice keeps the head of the list, so the ordering is what
        # decides which reports survive.
        db = _FakeDb()
        await apply_retention(db, REPORT, "s1")  # type: ignore[arg-type]
        assert "ORDER BY review_items.created_at DESC" in db.statements[0]


class TestTrashWindow:
    def test_the_deadline_is_thirty_days_after_trashing(self) -> None:
        item = _item(40, trashed_days=1)
        assert expires_at(item) == item.trashed_at + timedelta(days=TRASH_TTL_DAYS)

    def test_an_active_report_has_no_deadline(self) -> None:
        # None means "not in the trash" — not "expires now".
        assert expires_at(_item(1)) is None


class TestDefaults:
    def test_the_defaults_match_what_the_ui_states(self) -> None:
        # The sidebar tells the user "the newest 3" and "30 days" in prose.
        assert KEEP_LATEST == 3
        assert TRASH_TTL_DAYS == 30
