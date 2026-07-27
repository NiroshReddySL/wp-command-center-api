"""Watchdog precision: URL resolution, alert reconciliation, and version
compare — the accuracy contracts for enterprise-scale sites.

WP collection pagination itself (full crawl / incremental / live-ID listing)
is covered in test_wp_pagination.py — it's a connector-level concern shared
by sync, ContentScorer, SEOAnalyzer, and LinkChecker, not watchdog-specific.
"""
from app.agents.watchdog.link_checker import _LinkExtractor, _plan_reconcile, _resolve_href
from app.agents.watchdog.plugin_audit import _version_lt

BASE = "https://www.example.com"


class TestResolveHref:
    def test_protocol_relative_is_external_not_a_local_path(self) -> None:
        # '//cdn.example.org/x' used to become 'https://site//cdn.example.org/x'
        # — a guaranteed 404 false positive
        assert _resolve_href("//cdn.example.org/lib.js", BASE) == "https://cdn.example.org/lib.js"

    def test_absolute_path_joins_site(self) -> None:
        assert _resolve_href("/pricing/", BASE) == f"{BASE}/pricing/"

    def test_relative_path_resolves(self) -> None:
        assert _resolve_href("guides/setup", BASE) == f"{BASE}/guides/setup"

    def test_fragment_is_stripped(self) -> None:
        assert _resolve_href(f"{BASE}/faq#pricing", BASE) == f"{BASE}/faq"

    def test_non_http_schemes_skipped(self) -> None:
        assert _resolve_href("ftp://files.example.com/a", BASE) is None
        assert _resolve_href("", BASE) is None

    def test_extractor_skips_inert_hrefs(self) -> None:
        parser = _LinkExtractor()
        parser.feed(
            '<a href="#top">t</a><a href="mailto:x@y.z">m</a>'
            '<a href="data:text/plain,hi">d</a><a href="https://ok.com/a">ok</a>'
        )
        assert parser.links == ["https://ok.com/a"]


class TestPlanReconcile:
    def test_new_broken_link_is_created(self) -> None:
        create, update, delete = _plan_reconcile(existing=set(), broken={"u1"}, checked={"u1"})
        assert create == {"u1"} and not update and not delete

    def test_still_broken_updates_not_recreates(self) -> None:
        # Dismissed/acknowledged alerts keep their status because the row survives
        create, update, delete = _plan_reconcile(existing={"u1"}, broken={"u1"}, checked={"u1"})
        assert update == {"u1"} and not create and not delete

    def test_fixed_link_deletes_alert(self) -> None:
        create, update, delete = _plan_reconcile(existing={"u1"}, broken=set(), checked={"u1"})
        assert delete == {"u1"} and not create and not update

    def test_unchecked_url_is_untouched(self) -> None:
        # Beyond the per-run cap: not verified this run — must NOT read as fixed
        create, update, delete = _plan_reconcile(existing={"u1"}, broken=set(), checked=set())
        assert not create and not update and not delete


class TestVersionCompare:
    def test_older_is_outdated(self) -> None:
        assert _version_lt("1.2.3", "1.3.0") is True

    def test_equal_is_not_outdated(self) -> None:
        assert _version_lt("6.7.1", "6.7.1") is False

    def test_locally_newer_is_not_outdated(self) -> None:
        # Rollbacks/betas: installed > wp.org latest must not flag "outdated"
        assert _version_lt("6.8.0", "6.7.2") is False

    def test_numeric_not_lexicographic(self) -> None:
        assert _version_lt("6.7.9", "6.7.10") is True


