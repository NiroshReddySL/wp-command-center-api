"""Regressions for the Watchdog audit.

Each class here pins one defect that made the module report the wrong thing
rather than crash — the failure mode that survives longest, because nothing
looks broken. The common thread is that "no alert" must only ever mean
"verified healthy", never "we didn't look" or "the run died".
"""
from sqlalchemy import String, column
from sqlalchemy.dialects import postgresql

from app.agents.base import BaseAgent
from app.agents.watchdog.link_checker import _classify, _resolve_href
from app.agents.watchdog.plugin_audit import _parse_version, _version_lt
from app.database.models import Alert
from app.services.site_scope import monitored

BASE = "https://www.example.com"
PAGE = "https://www.example.com/blog/2024/my-post/"


class _FakeDb:
    def add(self, _obj: object) -> None: ...
    async def flush(self) -> None: ...


class _RecordingAgent(BaseAgent):
    """Captures notifications instead of dispatching them to Teams."""

    def __init__(self) -> None:
        super().__init__(_FakeDb())  # type: ignore[arg-type]
        self.notified: list[Alert] = []

    async def _notify(self, alert: Alert) -> None:
        self.notified.append(alert)

    async def run(self, site_id: str) -> list[Alert]:
        return []


def _alert(severity: str) -> Alert:
    return Alert(
        site_id="s1", agent="watchdog", severity=severity, type="broken_link",
        title="t", description="d", metadata_={}, status="open",
    )


class TestMonitoredSiteScope:
    """A failed content sync sets Site.status = "error". The scheduler used to
    select `== "active"`, so that site silently dropped out of every scheduled
    run — while the manual re-run used `!= "inactive"` and still processed it.
    Hence "it only works when I press Re-run"."""

    def _sql(self) -> str:
        return str(monitored().compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        ))

    def test_scope_is_not_inactive_rather_than_equals_active(self) -> None:
        sql = self._sql()
        assert "!=" in sql and "inactive" in sql
        assert "= 'active'" not in sql

    def test_a_site_with_a_failed_sync_is_still_monitored(self) -> None:
        # "error" is set by content_sync on any sync failure and only cleared
        # by a later success — a bad app password would hide the site forever.
        assert "error" not in self._sql()


class TestEscalationNotifies:
    """Notification used to live only in `create_alert`, and every agent
    mutated `.severity` directly on existing rows — so the transition people
    actually care about (a link going 500 -> 404) was silent."""

    async def test_warning_to_critical_notifies(self) -> None:
        agent = _RecordingAgent()
        alert = _alert("warning")
        await agent.update_alert(alert, severity="critical", title="x", description="y")
        assert len(agent.notified) == 1
        assert alert.severity == "critical"

    async def test_staying_critical_does_not_re_notify(self) -> None:
        # Re-notifying every 6-hour run is how alerting gets muted.
        agent = _RecordingAgent()
        await agent.update_alert(_alert("critical"), severity="critical", title="x", description="y")
        assert agent.notified == []

    async def test_staying_warning_does_not_notify(self) -> None:
        agent = _RecordingAgent()
        await agent.update_alert(_alert("warning"), severity="warning", title="x", description="y")
        assert agent.notified == []

    async def test_de_escalation_does_not_notify(self) -> None:
        agent = _RecordingAgent()
        await agent.update_alert(_alert("critical"), severity="warning", title="x", description="y")
        assert agent.notified == []

    async def test_updates_fields_in_place_so_triage_survives(self) -> None:
        # created_at and an acknowledged/dismissed status must outlive a re-run.
        agent = _RecordingAgent()
        alert = _alert("warning")
        alert.status = "acknowledged"
        await agent.update_alert(
            alert, severity="warning", title="new title",
            description="new desc", metadata={"url": "u"},
        )
        assert alert.status == "acknowledged"
        assert (alert.title, alert.description, alert.metadata_) == (
            "new title", "new desc", {"url": "u"},
        )

    async def test_title_is_bounded_to_the_column(self) -> None:
        agent = _RecordingAgent()
        alert = _alert("warning")
        await agent.update_alert(alert, severity="warning", title="x" * 900, description="d")
        assert len(alert.title) == 500

    async def test_metadata_is_left_alone_when_not_supplied(self) -> None:
        agent = _RecordingAgent()
        alert = _alert("warning")
        alert.metadata_ = {"keep": 1}
        await agent.update_alert(alert, severity="warning", title="t", description="d")
        assert alert.metadata_ == {"keep": 1}


class TestRelativeHrefResolvesAgainstThePage:
    """Document-relative hrefs resolve against the document, per the HTML
    spec. Resolving them against the site root produced confident 404s for
    links that open fine in a browser."""

    def test_dot_dot_resolves_against_the_containing_page(self) -> None:
        assert _resolve_href("../other/", BASE, PAGE) == f"{BASE}/blog/2024/other/"

    def test_bare_relative_resolves_against_the_containing_page(self) -> None:
        assert _resolve_href("setup", BASE, PAGE) == f"{BASE}/blog/2024/my-post/setup"

    def test_root_relative_still_uses_the_site_root(self) -> None:
        assert _resolve_href("/pricing/", BASE, PAGE) == f"{BASE}/pricing/"

    def test_protocol_relative_is_still_external(self) -> None:
        assert _resolve_href("//cdn.example.org/a.js", BASE, PAGE) == "https://cdn.example.org/a.js"

    def test_absolute_url_is_untouched(self) -> None:
        assert _resolve_href("https://other.com/x", BASE, PAGE) == "https://other.com/x"

    def test_falls_back_to_site_root_without_a_page_url(self) -> None:
        assert _resolve_href("guides/setup", BASE) == f"{BASE}/guides/setup"

    def test_a_non_http_page_url_is_not_trusted_as_a_base(self) -> None:
        assert _resolve_href("setup", BASE, "not-a-url") == f"{BASE}/setup"


class TestInternalAuthWallIsBroken:
    """A 403 from a third party is usually bot protection. A 403 from your own
    site is a page your readers cannot open."""

    def test_internal_401_and_403_are_reported(self) -> None:
        assert _classify(401, is_internal=True) == "warning"
        assert _classify(403, is_internal=True) == "warning"

    def test_external_401_and_403_stay_forgiven(self) -> None:
        assert _classify(401, is_internal=False) is None
        assert _classify(403, is_internal=False) is None

    def test_rate_limiting_is_forgiven_on_both(self) -> None:
        # 429/503 are transient by definition — never a broken link.
        for internal in (True, False):
            assert _classify(429, internal) is None
            assert _classify(503, internal) is None

    def test_genuine_404_still_critical(self) -> None:
        assert _classify(404, is_internal=True) == "critical"
        assert _classify(404, is_internal=False) == "critical"

    def test_healthy_responses_are_not_broken(self) -> None:
        for status in (200, 301, 302):
            assert _classify(status, is_internal=True) is None


class TestVersionPrerelease:
    """Non-numeric segments used to be dropped, so "2.0.0-rc1" parsed to
    (2, 0) and padded back to (2, 0, 0) — indistinguishable from the stable
    2.0.0, and a site on a release candidate reported as up to date."""

    def test_release_candidate_is_older_than_its_release(self) -> None:
        assert _version_lt("2.0.0-rc1", "2.0.0") is True

    def test_beta_is_older_than_its_release(self) -> None:
        assert _version_lt("3.0.1-beta", "3.0.1") is True

    def test_release_is_not_older_than_its_own_prerelease(self) -> None:
        assert _version_lt("2.0.0", "2.0.0-rc1") is False

    def test_prereleases_order_among_themselves(self) -> None:
        assert _version_lt("2.1-beta", "2.1-rc") is True

    def test_parses_release_and_suffix_separately(self) -> None:
        assert _parse_version("2.0.0-rc1") == ((2, 0, 0), "rc1")
        assert _parse_version("6.7.1") == ((6, 7, 1), "")

    def test_leading_v_is_tolerated(self) -> None:
        assert _version_lt("v1.2.3", "v1.3.0") is True

    def test_junk_versions_do_not_raise(self) -> None:
        for a, b in (("", "1.0"), ("abc", "1.0"), ("1.0", ""), ("", "")):
            assert isinstance(_version_lt(a, b), bool)

    def test_existing_contracts_still_hold(self) -> None:
        assert _version_lt("1.2.3", "1.3.0") is True
        assert _version_lt("6.7.1", "6.7.1") is False
        assert _version_lt("6.8.0", "6.7.2") is False   # local rollback
        assert _version_lt("6.7.9", "6.7.10") is True   # numeric, not lexicographic


class TestAlertTypeFilterEscaping:
    """`type` is a raw client query param and SQLAlchemy's `contains()`
    compiles to LIKE, where `_` matches any single character."""

    def _sql(self, autoescape: bool) -> str:
        expr = column("type", String).contains("broken_link", autoescape=autoescape)
        return str(expr.compile(
            dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}
        ))

    def test_underscore_is_escaped(self) -> None:
        # SQLAlchemy picks its own escape character and emits an ESCAPE
        # clause, so assert the effect rather than the exact spelling: the
        # bare literal no longer reaches the LIKE pattern.
        sql = self._sql(autoescape=True)
        assert "ESCAPE" in sql
        assert "'broken_link'" not in sql

    def test_unescaped_form_leaves_the_wildcard_live(self) -> None:
        # Documents what the old call did: `_` stayed a LIKE wildcard, so
        # `type=_` matched every alert.
        sql = self._sql(autoescape=False)
        assert "ESCAPE" not in sql
        assert "'broken_link'" in sql
