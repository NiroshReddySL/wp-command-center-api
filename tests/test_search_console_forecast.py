"""Search Console enrichment of the traffic forecast.

Why GSC and not, say, Google Trends: impressions are a genuine LEADING
indicator for this specific site (they move before clicks do), they're
absolute counts rather than a relative 0-100 index, and they split traffic
into demand (impressions) vs. conversion (CTR/position) so a drop becomes
attributable instead of merely visible.

Two properties matter most here and are easy to get wrong:
  - GSC finalizes ~2-3 days behind analytics, so the most recent days must
    render BLANK, never 0 — a zero would read to the model as "search
    traffic collapsed" on exactly the days it weights most heavily.
  - GSC is an ENRICHMENT: a site with no verified property must still get
    the traffic-only forecast that worked before, not an error.
"""
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.ai.prompts import traffic_prediction_prompt
from app.connectors.search_console import SearchConsoleConnector
from app.services.traffic_prediction import _build_history_csv


class _Snap:
    """Minimal stand-in for TrafficSnapshot — _build_history_csv only ever
    reads these four attributes, so this stays a pure, DB-free test."""

    def __init__(self, date: str, pageviews: int, sessions: int, users: int) -> None:
        self.date = date
        self.pageviews = pageviews
        self.sessions = sessions
        self.users = users


def _search(impressions: int, clicks: int, ctr: float, position: float) -> dict:
    return {"impressions": impressions, "clicks": clicks, "ctr": ctr, "position": position}


class TestBuildHistoryCsv:
    def test_no_search_data_produces_the_original_traffic_only_csv(self) -> None:
        snaps = [_Snap("2026-07-28", 535, 312, 262), _Snap("2026-07-29", 452, 320, 274)]
        csv = _build_history_csv(snaps, {})
        assert csv == (
            "date,pageviews,sessions,users\n"
            "2026-07-28,535,312,262\n"
            "2026-07-29,452,320,274"
        )

    def test_search_columns_are_appended_when_available(self) -> None:
        snaps = [_Snap("2026-07-28", 535, 312, 262)]
        csv = _build_history_csv(snaps, {"2026-07-28": _search(4210, 180, 4.28, 12.4)})
        assert csv.splitlines()[0] == (
            "date,pageviews,sessions,users,impressions,clicks,ctr_pct,avg_position"
        )
        assert csv.splitlines()[1] == "2026-07-28,535,312,262,4210,180,4.28,12.4"

    def test_gsc_lagging_days_render_blank_not_zero(self) -> None:
        # The whole point: 0 would tell the model search traffic died on the
        # most recent (most heavily weighted) day.
        snaps = [_Snap("2026-07-28", 535, 312, 262), _Snap("2026-07-29", 452, 320, 274)]
        csv = _build_history_csv(snaps, {"2026-07-28": _search(4210, 180, 4.28, 12.4)})
        lagging_row = csv.splitlines()[2]
        assert lagging_row == "2026-07-29,452,320,274,,,,"
        assert ",0,0," not in lagging_row

    def test_a_day_with_genuinely_zero_search_traffic_is_still_written_as_zero(self) -> None:
        # Distinct from the blank case above — GSC reported this day, and it
        # really was zero. That's data, and must not be blanked out.
        snaps = [_Snap("2026-07-28", 12, 9, 8)]
        csv = _build_history_csv(snaps, {"2026-07-28": _search(0, 0, 0.0, 0.0)})
        assert csv.splitlines()[1] == "2026-07-28,12,9,8,0,0,0.0,0.0"

    def test_search_days_with_no_matching_snapshot_are_ignored(self) -> None:
        # The traffic series drives the rows; a GSC-only date has no
        # pageviews to forecast from and must not invent a row.
        snaps = [_Snap("2026-07-28", 535, 312, 262)]
        csv = _build_history_csv(snaps, {
            "2026-07-28": _search(4210, 180, 4.28, 12.4),
            "2026-07-27": _search(3900, 150, 3.85, 13.1),
        })
        assert len(csv.splitlines()) == 2  # header + the one snapshot day
        assert "2026-07-27" not in csv

    def test_empty_snapshots_still_emits_a_header(self) -> None:
        assert _build_history_csv([], {}) == "date,pageviews,sessions,users"


class TestTrafficPredictionPrompt:
    def test_search_guidance_is_absent_without_gsc_data(self) -> None:
        prompt = traffic_prediction_prompt("Site", "date,pageviews\n2026-07-28,1", 7)
        assert "Search Console" not in prompt
        assert "LEADING INDICATORS" not in prompt

    def test_search_guidance_appears_with_gsc_data(self) -> None:
        prompt = traffic_prediction_prompt(
            "Site", "date,pageviews\n2026-07-28,1", 7, has_search_data=True
        )
        assert "LEADING INDICATORS" in prompt
        assert "never as zero" in prompt  # the blank-vs-zero instruction
        assert "DEMAND changes" in prompt  # narrative must split demand vs conversion

    def test_horizon_is_carried_into_the_rules_either_way(self) -> None:
        for has_search in (True, False):
            prompt = traffic_prediction_prompt("Site", "csv", 14, has_search_data=has_search)
            assert "EXACTLY 14 entries" in prompt


def _gsc_daily_response(rows: list[tuple[str, int, int, float, float]]) -> httpx.Response:
    """rows: (date "YYYY-MM-DD", clicks, impressions, ctr_ratio, position)."""
    return httpx.Response(
        200,
        json={
            "rows": [
                {
                    "keys": [date],
                    "clicks": clicks,
                    "impressions": impressions,
                    "ctr": ctr,
                    "position": position,
                }
                for date, clicks, impressions, ctr, position in rows
            ]
        },
        request=httpx.Request("POST", "https://www.googleapis.com/webmasters/v3/x/searchAnalytics/query"),
    )


class TestGetDailySearchMetrics:
    @pytest.mark.asyncio
    async def test_parses_rows_and_converts_ctr_to_percent(self) -> None:
        resp = _gsc_daily_response([("2026-07-28", 180, 4210, 0.0428, 12.44)])
        with patch.object(httpx.AsyncClient, "post", new=AsyncMock(return_value=resp)):
            result = await SearchConsoleConnector("token").get_daily_search_metrics("https://x.com/")
        assert result == [
            {"date": "2026-07-28", "clicks": 180, "impressions": 4210, "ctr": 4.28, "position": 12.4}
        ]

    @pytest.mark.asyncio
    async def test_rows_are_sorted_oldest_first(self) -> None:
        resp = _gsc_daily_response([
            ("2026-07-29", 5, 100, 0.05, 9.0),
            ("2026-07-27", 3, 80, 0.0375, 11.0),
            ("2026-07-28", 4, 90, 0.0444, 10.0),
        ])
        with patch.object(httpx.AsyncClient, "post", new=AsyncMock(return_value=resp)):
            result = await SearchConsoleConnector("token").get_daily_search_metrics("https://x.com/")
        assert [r["date"] for r in result] == ["2026-07-27", "2026-07-28", "2026-07-29"]

    @pytest.mark.asyncio
    async def test_empty_response_returns_empty_list(self) -> None:
        resp = _gsc_daily_response([])
        with patch.object(httpx.AsyncClient, "post", new=AsyncMock(return_value=resp)):
            result = await SearchConsoleConnector("token").get_daily_search_metrics("https://x.com/")
        assert result == []

    @pytest.mark.asyncio
    async def test_requests_the_date_dimension(self) -> None:
        resp = _gsc_daily_response([])
        mock_post = AsyncMock(return_value=resp)
        with patch.object(httpx.AsyncClient, "post", new=mock_post):
            await SearchConsoleConnector("token").get_daily_search_metrics("https://x.com/", days=90)
        body = mock_post.call_args.kwargs["json"]
        assert body["dimensions"] == ["date"]
        # Must exceed the number of days requested or the tail gets truncated.
        assert body["rowLimit"] >= 90

    @pytest.mark.asyncio
    async def test_site_url_is_encoded_into_the_path(self) -> None:
        resp = _gsc_daily_response([])
        mock_post = AsyncMock(return_value=resp)
        with patch.object(httpx.AsyncClient, "post", new=mock_post):
            await SearchConsoleConnector("token").get_daily_search_metrics("https://x.com/")
        called_url = mock_post.call_args.args[0]
        assert "https%3A%2F%2Fx.com%2F" in called_url
