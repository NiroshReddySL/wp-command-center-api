"""Traffic Agent's GA4 historical backfill — a site's history already
exists in GA4 itself, so predictions don't need to wait for the nightly
agent to accumulate 14+ calendar days one at a time, and a day previously
stored as "estimated" (no GA4 connected at the time) can be upgraded to
real numbers wherever GA4 has data for that date.

Also covers a real bug found while building this: the day-over-day
comparison used to re-derive "yesterday" from `datetime.now()` independently
of the snapshot it had just persisted — since GA4 snapshots are always
dated yesterday already, this compared the freshly-written row against
itself (always 0% change, so traffic_drop/traffic_spike alerts never
actually fired for GA4-connected sites).
"""
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.agents.traffic.traffic_agent import (
    _previous_day,
    _should_attempt_backfill,
    _should_write_snapshot,
)
from app.connectors.analytics import AnalyticsConnector


class TestPreviousDay:
    def test_returns_the_day_before(self) -> None:
        assert _previous_day("2026-07-30") == "2026-07-29"

    def test_crosses_a_month_boundary(self) -> None:
        assert _previous_day("2026-08-01") == "2026-07-31"

    def test_crosses_a_year_boundary(self) -> None:
        assert _previous_day("2026-01-01") == "2025-12-31"


class TestShouldWriteSnapshot:
    def test_writes_when_nothing_stored_yet(self) -> None:
        assert _should_write_snapshot(None, "estimated") is True
        assert _should_write_snapshot(None, "ga4") is True

    def test_ga4_can_upgrade_an_estimated_day(self) -> None:
        assert _should_write_snapshot("estimated", "ga4") is True

    def test_estimated_never_downgrades_a_real_ga4_day(self) -> None:
        assert _should_write_snapshot("ga4", "estimated") is False

    def test_same_source_refresh_is_allowed(self) -> None:
        assert _should_write_snapshot("ga4", "ga4") is True
        assert _should_write_snapshot("estimated", "estimated") is True


class TestShouldAttemptBackfill:
    def test_skips_when_history_is_already_full_and_real(self) -> None:
        sources = ["ga4"] * 14
        assert _should_attempt_backfill(sources, min_snapshots=14) is False

    def test_attempts_when_fewer_ga4_days_than_the_minimum(self) -> None:
        sources = ["ga4"] * 5
        assert _should_attempt_backfill(sources, min_snapshots=14) is True

    def test_attempts_when_an_estimated_day_could_be_upgraded_even_if_otherwise_full(self) -> None:
        sources = ["ga4"] * 13 + ["estimated"]
        assert _should_attempt_backfill(sources, min_snapshots=14) is True

    def test_attempts_when_theres_no_history_at_all(self) -> None:
        assert _should_attempt_backfill([], min_snapshots=14) is True


def _daily_site_metrics_response(rows: list[tuple[str, int, int, int, float, float]]) -> httpx.Response:
    """rows: (raw_date "YYYYMMDD", pageviews, sessions, users, bounce_rate_ratio, avg_duration)."""
    return httpx.Response(
        200,
        json={
            "rows": [
                {
                    "dimensionValues": [{"value": raw_date}],
                    "metricValues": [
                        {"value": str(pv)}, {"value": str(sessions)}, {"value": str(users)},
                        {"value": str(bounce)}, {"value": str(duration)},
                    ],
                }
                for raw_date, pv, sessions, users, bounce, duration in rows
            ]
        },
        request=httpx.Request("POST", "https://analyticsdata.googleapis.com/v1beta/x:runReport"),
    )


class TestGetDailySiteMetrics:
    @pytest.mark.asyncio
    async def test_parses_one_row_per_day(self) -> None:
        resp = _daily_site_metrics_response([
            ("20260715", 100, 80, 60, 0.5, 45.0),
            ("20260716", 120, 90, 70, 0.4, 50.0),
        ])
        with patch.object(httpx.AsyncClient, "post", new=AsyncMock(return_value=resp)):
            result = await AnalyticsConnector("token").get_daily_site_metrics("properties/123", days=90)

        assert result == [
            {"date": "2026-07-15", "pageviews": 100, "sessions": 80, "users": 60, "bounce_rate": 50.0, "avg_session_duration": 45.0},
            {"date": "2026-07-16", "pageviews": 120, "sessions": 90, "users": 70, "bounce_rate": 40.0, "avg_session_duration": 50.0},
        ]

    @pytest.mark.asyncio
    async def test_bounce_rate_converted_from_ratio_to_percent(self) -> None:
        resp = _daily_site_metrics_response([("20260715", 10, 8, 6, 0.735, 12.3)])
        with patch.object(httpx.AsyncClient, "post", new=AsyncMock(return_value=resp)):
            result = await AnalyticsConnector("token").get_daily_site_metrics("properties/123")
        assert result[0]["bounce_rate"] == 73.5

    @pytest.mark.asyncio
    async def test_empty_response_returns_empty_list(self) -> None:
        resp = _daily_site_metrics_response([])
        with patch.object(httpx.AsyncClient, "post", new=AsyncMock(return_value=resp)):
            result = await AnalyticsConnector("token").get_daily_site_metrics("properties/123")
        assert result == []

    @pytest.mark.asyncio
    async def test_uses_date_dimension_and_ends_yesterday(self) -> None:
        resp = _daily_site_metrics_response([])
        mock_post = AsyncMock(return_value=resp)
        with patch.object(httpx.AsyncClient, "post", new=mock_post):
            await AnalyticsConnector("token").get_daily_site_metrics("properties/123", days=90)
        body = mock_post.call_args.kwargs["json"]
        assert body["dimensions"] == [{"name": "date"}]
        assert body["dateRanges"] == [{"startDate": "90daysAgo", "endDate": "yesterday"}]

    @pytest.mark.asyncio
    async def test_bare_property_id_gets_prefixed(self) -> None:
        resp = _daily_site_metrics_response([])
        mock_post = AsyncMock(return_value=resp)
        with patch.object(httpx.AsyncClient, "post", new=mock_post):
            await AnalyticsConnector("token").get_daily_site_metrics("123456")
        called_url = mock_post.call_args.args[0]
        assert called_url.endswith("properties/123456:runReport")
