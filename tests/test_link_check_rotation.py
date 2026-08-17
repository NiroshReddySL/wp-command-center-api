"""Which links a run verifies.

The checker could only afford a few hundred HTTP requests per run, and chose
them with `(sorted(internal) + sorted(external))[:cap]`. Two separate defects
hid in that one line, and both produced the same symptom: a dashboard
reporting zero broken links because nothing had looked.

Starvation — concatenating the classes before slicing means a site with more
internal links than the entire budget never checks a single external one. On
the install this was found on: 1,294 internal links against a budget of 500,
so all 803 external links were permanently unverified.

No rotation — a sorted slice is the same slice every run. 500 of 2,097 links
were checked forever, always the same alphabetical window, and 76% of the
site's links had never been verified once.
"""
from datetime import UTC, datetime, timedelta

from app.agents.watchdog.link_checker import plan_link_batch

NOW = datetime(2026, 8, 14, tzinfo=UTC)


def _ago(**kw: int) -> datetime:
    return NOW - timedelta(**kw)


def _urls(prefix: str, n: int) -> list[str]:
    return [f"https://{prefix}.example/{i:04d}" for i in range(n)]


class TestExternalLinksAreNeverStarved:
    def test_external_links_get_a_share_when_internal_could_fill_the_budget(self) -> None:
        internal, external = _urls("site", 1294), _urls("other", 803)
        planned = plan_link_batch(internal, external, {}, budget=500)

        externals = [u for u, is_int in planned if not is_int]
        assert len(planned) == 500
        assert len(externals) == 200      # the 40% reserve
        # The exact defect: zero external links checked.
        assert externals, "external links were starved by internal ones"

    def test_the_share_is_configurable(self) -> None:
        planned = plan_link_batch(_urls("s", 100), _urls("e", 100), {}, budget=10,
                                  external_share=0.5)
        assert sum(1 for _, i in planned if not i) == 5

    def test_unused_internal_budget_flows_to_external(self) -> None:
        # A site with few internal links must not leave the budget unspent.
        planned = plan_link_batch(_urls("s", 10), _urls("e", 900), {}, budget=100)
        assert len(planned) == 100
        assert sum(1 for _, i in planned if i) == 10

    def test_unused_external_budget_flows_to_internal(self) -> None:
        planned = plan_link_batch(_urls("s", 900), _urls("e", 10), {}, budget=100)
        assert len(planned) == 100
        assert sum(1 for _, i in planned if not i) == 10

    def test_a_small_site_is_checked_entirely(self) -> None:
        planned = plan_link_batch(_urls("s", 30), _urls("e", 20), {}, budget=500)
        assert len(planned) == 50


class TestRotation:
    def test_never_checked_links_come_first(self) -> None:
        internal = ["https://site.example/old", "https://site.example/new"]
        last = {"https://site.example/old": _ago(hours=1)}
        planned = plan_link_batch(internal, [], last, budget=1)
        assert planned == [("https://site.example/new", True)]

    def test_the_stalest_checked_link_is_next(self) -> None:
        internal = ["https://site.example/a", "https://site.example/b"]
        last = {
            "https://site.example/a": _ago(hours=1),
            "https://site.example/b": _ago(days=30),
        }
        planned = plan_link_batch(internal, [], last, budget=1)
        assert planned == [("https://site.example/b", True)]

    def test_repeating_the_run_reaches_new_links(self) -> None:
        # The property the old code lacked: pressing on covers more ground
        # rather than re-verifying one alphabetical window.
        internal = _urls("site", 20)
        first = [u for u, _ in plan_link_batch(internal, [], {}, budget=10)]
        after = dict.fromkeys(first, NOW)
        second = [u for u, _ in plan_link_batch(internal, [], after, budget=10)]
        assert not set(first) & set(second)
        assert set(first) | set(second) == set(internal)

    def test_a_full_sweep_covers_everything(self) -> None:
        links, checked, seen = _urls("site", 100), {}, set()
        for _ in range(10):
            batch = [u for u, _ in plan_link_batch(links, [], checked, budget=10)]
            seen.update(batch)
            checked.update(dict.fromkeys(batch, NOW + timedelta(seconds=len(seen))))
        assert seen == set(links)

    def test_ordering_is_deterministic_when_timestamps_tie(self) -> None:
        # A run stamps every link it checked with the same instant, so ties are
        # the normal case, not an edge case.
        links = ["https://site.example/b", "https://site.example/a"]
        last = dict.fromkeys(links, _ago(days=1))
        assert plan_link_batch(links, [], last, budget=1) == [("https://site.example/a", True)]


class TestMalformedLinks:
    """An href containing prose is a certainty, not a maybe.

    Someone pastes a sentence into WordPress's link field and the live page
    carries `http://A%20manual%20approach%20may%20work...` as a link. Treated
    as an ordinary external failure it gets the benefit of the doubt reserved
    for third-party flakiness — reported as a warning that might resolve
    itself. It never will.
    """

    def test_prose_pasted_into_a_link_field_is_malformed(self) -> None:
        from app.agents.watchdog.link_checker import is_malformed_host
        assert is_malformed_host(
            "http://A%20manual%20approach%20may%20work%20for%20companies%20requiring"
        )

    def test_ordinary_urls_are_not(self) -> None:
        from app.agents.watchdog.link_checker import is_malformed_host
        for url in (
            "https://www.cloudfuze.com/pricing/",
            "http://example.com:8080/path",
            "https://sub.domain.co.uk/a?b=c#d",
            "http://[2001:db8::1]/x",
            "https://xn--80ak6aa92e.com/",      # punycode IDN
        ):
            assert not is_malformed_host(url), url

    def test_spaces_and_commas_in_a_host_are_malformed(self) -> None:
        from app.agents.watchdog.link_checker import is_malformed_host
        assert is_malformed_host("https://exa mple.com/")
        assert is_malformed_host("http://foo,bar.com/")

    def test_a_hostless_url_is_malformed(self) -> None:
        from app.agents.watchdog.link_checker import is_malformed_host
        assert is_malformed_host("http:///just-a-path")


class TestBounds:
    def test_duplicates_are_collapsed(self) -> None:
        planned = plan_link_batch(["https://s.example/a"] * 5, [], {}, budget=10)
        assert len(planned) == 1

    def test_a_zero_budget_checks_nothing(self) -> None:
        assert plan_link_batch(_urls("s", 5), _urls("e", 5), {}, budget=0) == []

    def test_no_links_is_not_an_error(self) -> None:
        assert plan_link_batch([], [], {}, budget=500) == []

    def test_the_budget_is_never_exceeded(self) -> None:
        planned = plan_link_batch(_urls("s", 5000), _urls("e", 5000), {}, budget=500)
        assert len(planned) == 500
