"""The notification bell's scope and its count.

Two properties, both about a number meaning what it says.

Scope: every other view honours the site picker, so a bell that does not is a
badge you cannot attribute — you cannot tell which site it is about, and
clearing the findings on screen never moves it.

Agreement: the list and the count must be built from one definition. A badge
reading 7 above a list of 3 is a bug report, and two queries drifting apart is
how it happens — which is why `_scoped` exists rather than each endpoint
writing its own filter.
"""
from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql

from app.api.notifications import NOTIFY_SEVERITIES, RECENT_LIMIT, _scoped
from app.database.models import Alert


def _sql(statement) -> str:
    return str(statement.compile(
        dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
    ))


def _list_query(site_id: str | None) -> str:
    return _sql(_scoped(select(Alert), site_id, open_only=False))


def _count_query(site_id: str | None) -> str:
    return _sql(_scoped(select(func.count(Alert.id)), site_id, open_only=True))


class TestScope:
    def test_a_selected_site_constrains_the_list(self) -> None:
        assert "site_id = 'site-1'" in _list_query("site-1")

    def test_a_selected_site_constrains_the_count(self) -> None:
        # The badge is the number people act on; scoping the list alone would
        # leave it counting sites the user is not looking at.
        assert "site_id = 'site-1'" in _count_query("site-1")

    def test_no_selection_spans_every_site(self) -> None:
        assert "site_id =" not in _list_query(None)
        assert "site_id =" not in _count_query(None)


class TestWhatCounts:
    def test_only_severities_worth_interrupting_someone(self) -> None:
        # "info" findings are for reading at leisure, not for a red badge.
        assert NOTIFY_SEVERITIES == ("critical", "warning")
        for query in (_list_query(None), _count_query(None)):
            assert "'critical'" in query and "'warning'" in query
            assert "'info'" not in query

    def test_the_badge_counts_only_unread(self) -> None:
        # Acknowledged alerts stay in the list — they are still open findings —
        # but they must not keep the badge lit.
        assert "status = 'open'" in _count_query(None)

    def test_the_list_keeps_acknowledged_items(self) -> None:
        assert "IN ('open', 'acknowledged')" in _list_query(None)

    def test_the_list_is_capped(self) -> None:
        # Which is exactly why the badge cannot be derived from it: the panel
        # showed 20 while 296 were open, and the number stopped moving.
        assert RECENT_LIMIT == 20
