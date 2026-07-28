"""Traffic module pure-function tests.

Covers the bugs found while modernizing the Traffic page:
  - `traffic_summary` used to silently show yesterday's numbers as "today"
    with a misleading 0.0% change when today's collection hadn't landed yet.
  - `top_pages` merged rows from different sites that share the same path
    (e.g. two sites both having "/about") into one row.
  - `traffic_trend` was hardcoded to pageviews only, with no way to chart
    sessions/users/bounce rate.
"""
from app.api.traffic import (
    _TREND_METRICS,
    _aggregate_geo,
    _aggregate_top_pages,
    _change_pct,
    _pick_anchor_date,
)


class TestPickAnchorDate:
    def test_prefers_today_when_present(self) -> None:
        assert _pick_anchor_date({"2026-07-28", "2026-07-27"}, "2026-07-28", "2026-07-27") == "2026-07-28"

    def test_falls_back_to_yesterday_when_today_missing(self) -> None:
        assert _pick_anchor_date({"2026-07-27"}, "2026-07-28", "2026-07-27") == "2026-07-27"

    def test_none_when_neither_present(self) -> None:
        assert _pick_anchor_date({"2026-07-01"}, "2026-07-28", "2026-07-27") is None


class TestChangePct:
    def test_computes_percentage_change(self) -> None:
        pct, has_comparison = _change_pct(150, 100)
        assert pct == 50.0
        assert has_comparison is True

    def test_negative_change(self) -> None:
        pct, has_comparison = _change_pct(50, 100)
        assert pct == -50.0
        assert has_comparison is True

    def test_no_previous_value_reports_no_comparison_instead_of_0pct(self) -> None:
        # Previously this case was indistinguishable from a genuine 0% change.
        pct, has_comparison = _change_pct(100, None)
        assert pct == 0.0
        assert has_comparison is False

    def test_zero_previous_value_reports_no_comparison(self) -> None:
        pct, has_comparison = _change_pct(100, 0)
        assert has_comparison is False


class TestAggregateTopPages:
    def test_sums_views_for_the_same_page_within_one_site(self) -> None:
        rows = [
            ("site-a", "Site A", [{"path": "/about", "views": 10}]),
            ("site-a", "Site A", [{"path": "/about", "views": 20}]),
        ]
        result = _aggregate_top_pages(rows)
        assert len(result) == 1
        assert result[0]["views"] == 30
        assert result[0]["site_id"] == "site-a"

    def test_does_not_merge_the_same_path_across_different_sites(self) -> None:
        rows = [
            ("site-a", "Site A", [{"path": "/about", "views": 10}]),
            ("site-b", "Site B", [{"path": "/about", "views": 999}]),
        ]
        result = _aggregate_top_pages(rows)
        assert len(result) == 2
        views_by_site = {r["site_id"]: r["views"] for r in result}
        assert views_by_site == {"site-a": 10, "site-b": 999}

    def test_sorted_descending_by_views(self) -> None:
        rows = [
            ("site-a", "Site A", [{"path": "/low", "views": 5}, {"path": "/high", "views": 500}]),
        ]
        result = _aggregate_top_pages(rows)
        assert [r["path"] for r in result] == ["/high", "/low"]

    def test_falls_back_to_url_when_path_missing(self) -> None:
        rows = [("site-a", "Site A", [{"url": "https://example.com/x", "views": 3}])]
        result = _aggregate_top_pages(rows)
        assert result[0]["views"] == 3


class TestAggregateGeo:
    def test_sums_country_views_and_sessions_across_snapshots(self) -> None:
        snapshots = [
            {"countries": [{"country": "USA", "country_code": "US", "views": 10, "sessions": 8}]},
            {"countries": [{"country": "USA", "country_code": "US", "views": 5, "sessions": 4}]},
        ]
        result = _aggregate_geo(snapshots)
        assert result["countries"][0]["views"] == 15
        assert result["countries"][0]["sessions"] == 12
        assert result["countries"][0]["pct"] == 100.0

    def test_region_percentages_sum_to_100(self) -> None:
        snapshots = [
            {"regions": [{"region": "West", "views": 30}, {"region": "East", "views": 70}]},
        ]
        result = _aggregate_geo(snapshots)
        pcts = {r["region"]: r["pct"] for r in result["regions"]}
        assert pcts == {"West": 30.0, "East": 70.0}

    def test_merges_cities_with_the_same_name_and_country(self) -> None:
        snapshots = [
            {"cities": [{"city": "Austin", "country": "USA", "views": 4}]},
            {"cities": [{"city": "Austin", "country": "USA", "views": 6}]},
        ]
        result = _aggregate_geo(snapshots)
        assert len(result["cities"]) == 1
        assert result["cities"][0]["views"] == 10

    def test_empty_snapshots_do_not_raise_division_by_zero(self) -> None:
        result = _aggregate_geo([])
        assert result == {"countries": [], "regions": [], "cities": []}


class TestTrendMetrics:
    def test_supports_all_documented_metrics(self) -> None:
        assert set(_TREND_METRICS) == {
            "pageviews", "sessions", "users", "bounce_rate", "avg_session_duration",
        }

    def test_each_extractor_reads_the_matching_attribute(self) -> None:
        class FakeSnapshot:
            pageviews = 100
            sessions = 80
            users = 60
            bounce_rate = 45.5
            avg_session_duration = 120.0

        snap = FakeSnapshot()
        assert _TREND_METRICS["pageviews"](snap) == 100
        assert _TREND_METRICS["sessions"](snap) == 80
        assert _TREND_METRICS["users"](snap) == 60
        assert _TREND_METRICS["bounce_rate"](snap) == 45.5
        assert _TREND_METRICS["avg_session_duration"](snap) == 120.0
