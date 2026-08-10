"""Which pages a performance run measures.

The monitor used to take the homepage plus the three highest-traffic posts,
freshly chosen every run — so the same handful of pages were measured forever
and the rest of the library was never seen. On the install this was found on
that was 22 pages out of 2,492, re-measured 28 to 35 times each.

Coverage has to come from rotating across runs, because a run that tries to
cover an enterprise library in one pass is a run that never finishes.
"""
from datetime import UTC, datetime, timedelta

from app.agents.watchdog.performance import plan_batch

HOME = "https://example.com"
NOW = datetime(2026, 8, 7, tzinfo=UTC)
CUTOFF = NOW - timedelta(hours=72)


def _seen(**pages: int) -> dict[str, datetime]:
    """Pages keyed to how many hours ago they were last measured."""
    return {url.replace("_", "/"): NOW - timedelta(hours=h) for url, h in pages.items()}


class TestPlanBatch:
    def test_never_measured_pages_come_first(self) -> None:
        pages = [f"{HOME}/a", f"{HOME}/b", f"{HOME}/c"]
        last_seen = {f"{HOME}/a": NOW - timedelta(hours=200)}
        chosen, _, _ = plan_batch(HOME, pages, last_seen, CUTOFF, budget=3)
        # /b and /c have never been measured; /a has, however long ago.
        assert chosen[1:] == [f"{HOME}/b", f"{HOME}/c"]

    def test_stale_before_recent(self) -> None:
        pages = [f"{HOME}/recent", f"{HOME}/old"]
        last_seen = {
            f"{HOME}/recent": NOW - timedelta(hours=80),
            f"{HOME}/old": NOW - timedelta(hours=500),
        }
        chosen, _, _ = plan_batch(HOME, pages, last_seen, CUTOFF, budget=2)
        assert chosen[1] == f"{HOME}/old"

    def test_incoming_order_breaks_ties(self) -> None:
        # Candidates arrive in traffic order, so among equally stale pages the
        # busiest is measured first.
        pages = [f"{HOME}/busy", f"{HOME}/quiet"]
        chosen, _, _ = plan_batch(HOME, pages, {}, CUTOFF, budget=3)
        assert chosen == [HOME, f"{HOME}/busy", f"{HOME}/quiet"]

    def test_homepage_always_measured(self) -> None:
        # The most-visited page, and the one a regression matters most on.
        chosen, _, _ = plan_batch(HOME, [f"{HOME}/a"], {}, CUTOFF, budget=1)
        assert chosen == [HOME]

    def test_homepage_is_not_duplicated_from_the_pool(self) -> None:
        chosen, _, _ = plan_batch(HOME, [f"{HOME}/", f"{HOME}/a"], {}, CUTOFF, budget=5)
        assert chosen.count(HOME) == 1
        assert f"{HOME}/" not in chosen[1:]

    def test_budget_bounds_the_run(self) -> None:
        # A run has to stay short whatever the library size — that is the
        # whole reason coverage is spread across runs.
        pages = [f"{HOME}/p{i}" for i in range(500)]
        chosen, due, pool = plan_batch(HOME, pages, {}, CUTOFF, budget=12)
        assert len(chosen) == 12
        assert len(due) == 500 and len(pool) == 500

    def test_fresh_pages_are_skipped_so_the_rotation_advances(self) -> None:
        # Without this the same recently-measured pages would be picked again
        # and the tail of the library would never be reached.
        pages = [f"{HOME}/a", f"{HOME}/b"]
        last_seen = {f"{HOME}/a": NOW, f"{HOME}/b": NOW}
        chosen, due, _ = plan_batch(HOME, pages, last_seen, CUTOFF, budget=12)
        assert due == []
        assert chosen == [HOME]

    def test_duplicate_candidates_are_collapsed(self) -> None:
        pages = [f"{HOME}/a", f"{HOME}/a", f"{HOME}/b"]
        _, _, pool = plan_batch(HOME, pages, {}, CUTOFF, budget=12)
        assert pool == [f"{HOME}/a", f"{HOME}/b"]

    def test_an_empty_library_still_measures_the_homepage(self) -> None:
        chosen, due, pool = plan_batch(HOME, [], {}, CUTOFF, budget=12)
        assert chosen == [HOME] and due == [] and pool == []


class TestQuotaSettings:
    def test_concurrency_respects_the_keyless_ceiling(self) -> None:
        # Keyless PSI allows roughly 25 requests per 100 seconds per IP, so
        # extra parallelism only converts scores into 429s and TTFB estimates.
        from app.config import settings

        assert settings.PSI_CONCURRENCY <= settings.PSI_CONCURRENCY_WITH_KEY
        assert settings.PSI_CONCURRENCY <= 3

    def test_a_run_is_bounded(self) -> None:
        from app.config import settings

        assert 0 < settings.PSI_MAX_PAGES_PER_RUN <= 50
        assert settings.PSI_FRESH_HOURS > 0
