"""GA4 Realtime active-users-by-title — the join key available for matching
a watched URL is page TITLE (GA4's Realtime API has no page-path dimension),
and rows sharing a title must be summed rather than overwritten.
"""
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.connectors.analytics import AnalyticsConnector


def _realtime_response(rows: list[tuple[str, int]]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "rows": [
                {"dimensionValues": [{"value": title}], "metricValues": [{"value": str(count)}]}
                for title, count in rows
            ]
        },
        request=httpx.Request("POST", "https://analyticsdata.googleapis.com/v1beta/x:runRealtimeReport"),
    )


class TestGetRealtimeActiveUsersByTitle:
    @pytest.mark.asyncio
    async def test_maps_title_to_active_users(self) -> None:
        resp = _realtime_response([("Pricing", 3), ("About Us", 1)])
        with patch.object(httpx.AsyncClient, "post", new=AsyncMock(return_value=resp)):
            result = await AnalyticsConnector("token").get_realtime_active_users_by_title("properties/123")
        assert result == {"Pricing": 3, "About Us": 1}

    @pytest.mark.asyncio
    async def test_duplicate_titles_are_summed_not_overwritten(self) -> None:
        # Two rows for the same title differ on an implicit second dimension
        # bucket GA returns internally — must be combined, not last-write-wins.
        resp = _realtime_response([("Pricing", 3), ("Pricing", 2)])
        with patch.object(httpx.AsyncClient, "post", new=AsyncMock(return_value=resp)):
            result = await AnalyticsConnector("token").get_realtime_active_users_by_title("properties/123")
        assert result == {"Pricing": 5}

    @pytest.mark.asyncio
    async def test_no_rows_returns_empty_dict(self) -> None:
        resp = _realtime_response([])
        with patch.object(httpx.AsyncClient, "post", new=AsyncMock(return_value=resp)):
            result = await AnalyticsConnector("token").get_realtime_active_users_by_title("properties/123")
        assert result == {}

    @pytest.mark.asyncio
    async def test_bare_property_id_gets_prefixed(self) -> None:
        resp = _realtime_response([])
        mock_post = AsyncMock(return_value=resp)
        with patch.object(httpx.AsyncClient, "post", new=mock_post):
            await AnalyticsConnector("token").get_realtime_active_users_by_title("123456")
        called_url = mock_post.call_args.args[0]
        assert called_url.endswith("properties/123456:runRealtimeReport")


def _report_response(rows: list[tuple[str, int]]) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "rows": [
                {"dimensionValues": [{"value": path}], "metricValues": [{"value": str(count)}]}
                for path, count in rows
            ]
        },
        request=httpx.Request("POST", "https://analyticsdata.googleapis.com/v1beta/x:runReport"),
    )


class TestGetActiveUsersByPath:
    @pytest.mark.asyncio
    async def test_maps_path_to_active_users(self) -> None:
        resp = _report_response([("/pricing/", 4), ("/about/", 2)])
        with patch.object(httpx.AsyncClient, "post", new=AsyncMock(return_value=resp)):
            result = await AnalyticsConnector("token").get_active_users_by_path(
                "properties/123", ["/pricing/", "/about/"], "7daysAgo", "today"
            )
        assert result == {"/pricing/": 4, "/about/": 2}

    @pytest.mark.asyncio
    async def test_duplicate_paths_are_summed(self) -> None:
        resp = _report_response([("/pricing/", 4), ("/pricing/", 1)])
        with patch.object(httpx.AsyncClient, "post", new=AsyncMock(return_value=resp)):
            result = await AnalyticsConnector("token").get_active_users_by_path(
                "properties/123", ["/pricing/"], "today", "today"
            )
        assert result == {"/pricing/": 5}

    @pytest.mark.asyncio
    async def test_empty_paths_list_short_circuits_without_a_request(self) -> None:
        mock_post = AsyncMock()
        with patch.object(httpx.AsyncClient, "post", new=mock_post):
            result = await AnalyticsConnector("token").get_active_users_by_path(
                "properties/123", [], "today", "today"
            )
        assert result == {}
        mock_post.assert_not_called()

    @pytest.mark.asyncio
    async def test_uses_runreport_not_realtime_endpoint(self) -> None:
        resp = _report_response([])
        mock_post = AsyncMock(return_value=resp)
        with patch.object(httpx.AsyncClient, "post", new=mock_post):
            await AnalyticsConnector("token").get_active_users_by_path(
                "properties/123", ["/x/"], "today", "today"
            )
        called_url = mock_post.call_args.args[0]
        assert called_url.endswith("properties/123:runReport")

    @pytest.mark.asyncio
    async def test_date_range_and_path_filter_sent_in_request_body(self) -> None:
        resp = _report_response([])
        mock_post = AsyncMock(return_value=resp)
        with patch.object(httpx.AsyncClient, "post", new=mock_post):
            await AnalyticsConnector("token").get_active_users_by_path(
                "properties/123", ["/a/", "/b/"], "28daysAgo", "today"
            )
        body = mock_post.call_args.kwargs["json"]
        assert body["dateRanges"] == [{"startDate": "28daysAgo", "endDate": "today"}]
        assert body["dimensionFilter"]["filter"]["inListFilter"]["values"] == ["/a/", "/b/"]


def _daily_report_response(rows: list[tuple[str, str, int]]) -> httpx.Response:
    """rows: (path, YYYYMMDD, count) — GA4's raw on-the-wire date format."""
    return httpx.Response(
        200,
        json={
            "rows": [
                {
                    "dimensionValues": [{"value": path}, {"value": ga_date}],
                    "metricValues": [{"value": str(count)}],
                }
                for path, ga_date, count in rows
            ]
        },
        request=httpx.Request("POST", "https://analyticsdata.googleapis.com/v1beta/x:runReport"),
    )


class TestGetDailyActiveUsersByPath:
    @pytest.mark.asyncio
    async def test_reformats_ga4_date_to_iso_and_groups_by_path(self) -> None:
        resp = _daily_report_response([("/pricing/", "20260715", 3), ("/pricing/", "20260716", 5)])
        with patch.object(httpx.AsyncClient, "post", new=AsyncMock(return_value=resp)):
            result = await AnalyticsConnector("token").get_daily_active_users_by_path(
                "properties/123", ["/pricing/"], "2026-07-15", "2026-07-16"
            )
        assert result == {"/pricing/": {"2026-07-15": 3, "2026-07-16": 5}}

    @pytest.mark.asyncio
    async def test_multiple_paths_kept_separate(self) -> None:
        resp = _daily_report_response([("/pricing/", "20260715", 3), ("/about/", "20260715", 1)])
        with patch.object(httpx.AsyncClient, "post", new=AsyncMock(return_value=resp)):
            result = await AnalyticsConnector("token").get_daily_active_users_by_path(
                "properties/123", ["/pricing/", "/about/"], "2026-07-15", "2026-07-15"
            )
        assert result == {"/pricing/": {"2026-07-15": 3}, "/about/": {"2026-07-15": 1}}

    @pytest.mark.asyncio
    async def test_duplicate_path_and_date_rows_are_summed(self) -> None:
        resp = _daily_report_response([("/pricing/", "20260715", 3), ("/pricing/", "20260715", 2)])
        with patch.object(httpx.AsyncClient, "post", new=AsyncMock(return_value=resp)):
            result = await AnalyticsConnector("token").get_daily_active_users_by_path(
                "properties/123", ["/pricing/"], "2026-07-15", "2026-07-15"
            )
        assert result == {"/pricing/": {"2026-07-15": 5}}

    @pytest.mark.asyncio
    async def test_empty_paths_list_short_circuits_without_a_request(self) -> None:
        mock_post = AsyncMock()
        with patch.object(httpx.AsyncClient, "post", new=mock_post):
            result = await AnalyticsConnector("token").get_daily_active_users_by_path(
                "properties/123", [], "2026-07-15", "2026-07-15"
            )
        assert result == {}
        mock_post.assert_not_called()

    @pytest.mark.asyncio
    async def test_uses_date_and_pagepath_dimensions(self) -> None:
        resp = _daily_report_response([])
        mock_post = AsyncMock(return_value=resp)
        with patch.object(httpx.AsyncClient, "post", new=mock_post):
            await AnalyticsConnector("token").get_daily_active_users_by_path(
                "properties/123", ["/x/"], "2026-07-15", "2026-07-16"
            )
        body = mock_post.call_args.kwargs["json"]
        assert body["dimensions"] == [{"name": "pagePath"}, {"name": "date"}]


def _engagement_report_response(rows: list[tuple[str, float, float, float, float]]) -> httpx.Response:
    """rows: (path, userEngagementDuration, activeUsers, bounceRate, sessions)."""
    return httpx.Response(
        200,
        json={
            "rows": [
                {
                    "dimensionValues": [{"value": path}],
                    "metricValues": [
                        {"value": str(duration)}, {"value": str(users)},
                        {"value": str(bounce)}, {"value": str(sessions)},
                    ],
                }
                for path, duration, users, bounce, sessions in rows
            ]
        },
        request=httpx.Request("POST", "https://analyticsdata.googleapis.com/v1beta/x:runReport"),
    )


class TestGetEngagementMetricsByPath:
    """"Average engagement time per active user" isn't a raw GA4 metric —
    it's userEngagementDuration / activeUsers, computed here the same way
    GA4's own reports derive it."""

    @pytest.mark.asyncio
    async def test_computes_average_engagement_time_per_active_user(self) -> None:
        resp = _engagement_report_response([("/pricing/", 300.0, 10.0, 0.4, 12.0)])
        with patch.object(httpx.AsyncClient, "post", new=AsyncMock(return_value=resp)):
            result = await AnalyticsConnector("token").get_engagement_metrics_by_path(
                "properties/123", ["/pricing/"], "2026-07-15", "2026-07-21"
            )
        assert result["/pricing/"]["avg_engagement_time"] == 30.0

    @pytest.mark.asyncio
    async def test_bounce_rate_is_weighted_by_sessions_when_rows_share_a_path(self) -> None:
        # Two rows for the same path (an implicit secondary dimension GA
        # buckets internally) — naively averaging 0.2 and 0.8 would give
        # 0.5, but weighting by sessions (10 and 90) must skew toward 0.8.
        resp = _engagement_report_response([
            ("/pricing/", 100.0, 5.0, 0.2, 10.0),
            ("/pricing/", 900.0, 45.0, 0.8, 90.0),
        ])
        with patch.object(httpx.AsyncClient, "post", new=AsyncMock(return_value=resp)):
            result = await AnalyticsConnector("token").get_engagement_metrics_by_path(
                "properties/123", ["/pricing/"], "2026-07-15", "2026-07-21"
            )
        assert result["/pricing/"]["bounce_rate"] == pytest.approx(0.74)

    @pytest.mark.asyncio
    async def test_multiple_paths_kept_separate(self) -> None:
        resp = _engagement_report_response([
            ("/pricing/", 300.0, 10.0, 0.4, 12.0), ("/about/", 60.0, 4.0, 0.5, 5.0),
        ])
        with patch.object(httpx.AsyncClient, "post", new=AsyncMock(return_value=resp)):
            result = await AnalyticsConnector("token").get_engagement_metrics_by_path(
                "properties/123", ["/pricing/", "/about/"], "2026-07-15", "2026-07-21"
            )
        assert set(result.keys()) == {"/pricing/", "/about/"}
        assert result["/about/"]["avg_engagement_time"] == 15.0

    @pytest.mark.asyncio
    async def test_zero_active_users_and_sessions_returns_zero_not_a_division_error(self) -> None:
        resp = _engagement_report_response([("/empty/", 0.0, 0.0, 0.0, 0.0)])
        with patch.object(httpx.AsyncClient, "post", new=AsyncMock(return_value=resp)):
            result = await AnalyticsConnector("token").get_engagement_metrics_by_path(
                "properties/123", ["/empty/"], "2026-07-15", "2026-07-21"
            )
        assert result["/empty/"] == {"avg_engagement_time": 0.0, "bounce_rate": 0.0}

    @pytest.mark.asyncio
    async def test_empty_paths_list_short_circuits_without_a_request(self) -> None:
        mock_post = AsyncMock()
        with patch.object(httpx.AsyncClient, "post", new=mock_post):
            result = await AnalyticsConnector("token").get_engagement_metrics_by_path(
                "properties/123", [], "2026-07-15", "2026-07-21"
            )
        assert result == {}
        mock_post.assert_not_called()

    @pytest.mark.asyncio
    async def test_uses_correct_metrics_and_path_filter(self) -> None:
        resp = _engagement_report_response([])
        mock_post = AsyncMock(return_value=resp)
        with patch.object(httpx.AsyncClient, "post", new=mock_post):
            await AnalyticsConnector("token").get_engagement_metrics_by_path(
                "properties/123", ["/x/"], "2026-07-15", "2026-07-21"
            )
        body = mock_post.call_args.kwargs["json"]
        assert body["metrics"] == [
            {"name": "userEngagementDuration"}, {"name": "activeUsers"},
            {"name": "bounceRate"}, {"name": "sessions"},
        ]
        assert body["dimensionFilter"]["filter"]["inListFilter"]["values"] == ["/x/"]
