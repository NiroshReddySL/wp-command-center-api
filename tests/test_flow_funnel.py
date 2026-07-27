"""GA4 Funnel Reports connector — evaluates a flow category's ordered
page-pattern steps via GA4's v1alpha runFunnelReport endpoint.

Why this exists: GA4's standard Data API has no session-identifying
dimension at all — true per-session path reconstruction needs BigQuery
Export. Funnel Reports is the one GA4 Data API surface that can apply an
ORDERED step sequence server-side without that, at the cost of being
aggregate + user-scoped rather than a literal per-session classification
(see FlowCategorySnapshot's docstring). The exact request/response shape
here (funnelParameterFilter's field names, the "N. label" step-name
prefix, the "RESERVED_TOTAL" breakdown row) was verified against a real
GA4 property, not just documentation — this is a v1alpha endpoint.
"""
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.connectors.analytics import AnalyticsConnector, _build_funnel_step, _parse_step_index

_STEP_KWARGS = {"is_directly_followed": False, "within_seconds": None}


class TestBuildFunnelStep:
    def test_contains_match_type_maps_to_ga4_contains(self) -> None:
        step = _build_funnel_step({"label": "Pricing", "match_type": "contains", "pattern": "/pricing", **_STEP_KWARGS})
        filt = step["filterExpression"]["funnelEventFilter"]["funnelParameterFilterExpression"]["funnelParameterFilter"]
        assert filt["eventParameterName"] == "page_location"
        assert filt["stringFilter"] == {"value": "/pricing", "matchType": "CONTAINS", "caseSensitive": False}
        assert "isDirectlyFollowedBy" not in step
        assert "withinDurationFromPriorStep" not in step

    def test_exact_and_regex_match_types(self) -> None:
        exact = _build_funnel_step({"label": "X", "match_type": "exact", "pattern": "/x", **_STEP_KWARGS})
        regex = _build_funnel_step({"label": "Y", "match_type": "regex", "pattern": ".*", **_STEP_KWARGS})

        def match_type(step: dict) -> str:
            return step["filterExpression"]["funnelEventFilter"]["funnelParameterFilterExpression"]["funnelParameterFilter"]["stringFilter"]["matchType"]

        assert match_type(exact) == "EXACT"
        assert match_type(regex) == "FULL_REGEXP"

    def test_unknown_match_type_falls_back_to_contains(self) -> None:
        step = _build_funnel_step({"label": "Z", "match_type": "bogus", "pattern": "/z", **_STEP_KWARGS})
        filt = step["filterExpression"]["funnelEventFilter"]["funnelParameterFilterExpression"]["funnelParameterFilter"]
        assert filt["stringFilter"]["matchType"] == "CONTAINS"

    def test_is_directly_followed_and_within_seconds_included_when_set(self) -> None:
        step = _build_funnel_step({
            "label": "Checkout", "match_type": "contains", "pattern": "/checkout",
            "is_directly_followed": True, "within_seconds": 1800,
        })
        assert step["isDirectlyFollowedBy"] is True
        assert step["withinDurationFromPriorStep"] == "1800s"

    def test_event_name_is_always_page_view(self) -> None:
        step = _build_funnel_step({"label": "A", "match_type": "contains", "pattern": "/a", **_STEP_KWARGS})
        assert step["filterExpression"]["funnelEventFilter"]["eventName"] == "page_view"


class TestParseStepIndex:
    def test_parses_leading_one_indexed_number(self) -> None:
        assert _parse_step_index("1. Landing") == 0
        assert _parse_step_index("2. Pricing") == 1
        assert _parse_step_index("10. Tenth step") == 9

    def test_missing_prefix_defaults_to_zero(self) -> None:
        assert _parse_step_index("no numeric prefix") == 0


def _funnel_response(rows: list[tuple]) -> httpx.Response:
    """rows: (step_name, active_users, completion_rate, abandonments, abandonment_rate, breakdown_value|None)."""
    out_rows = []
    for step_name, active_users, completion_rate, abandonments, abandonment_rate, breakdown_value in rows:
        dim_values = [{"value": step_name}]
        if breakdown_value is not None:
            dim_values.append({"value": breakdown_value})
        out_rows.append({
            "dimensionValues": dim_values,
            "metricValues": [
                {"value": str(active_users)}, {"value": str(completion_rate)},
                {"value": str(abandonments)}, {"value": str(abandonment_rate)},
            ],
        })
    return httpx.Response(
        200, json={"funnelTable": {"rows": out_rows}},
        request=httpx.Request("POST", "https://analyticsdata.googleapis.com/v1alpha/x:runFunnelReport"),
    )


_TWO_STEPS = [
    {"label": "Step 1", "match_type": "contains", "pattern": "/a", **_STEP_KWARGS},
    {"label": "Step 2", "match_type": "contains", "pattern": "/b", **_STEP_KWARGS},
]


class TestRunFunnelReport:
    @pytest.mark.asyncio
    async def test_parses_step_results_without_breakdown(self) -> None:
        resp = _funnel_response([
            ("1. Step 1", 1000, 0.5, 500, 0.5, None),
            ("2. Step 2", 500, 1.0, 0, 0.0, None),
        ])
        with patch.object(httpx.AsyncClient, "post", new=AsyncMock(return_value=resp)):
            result = await AnalyticsConnector("token").run_funnel_report(
                "properties/123", _TWO_STEPS, "2026-07-01", "2026-07-21"
            )

        assert result["total_entered"] == 1000
        assert result["total_completed"] == 500
        assert result["conversion_rate"] == 0.5
        assert result["step_results"][0]["label"] == "Step 1"
        assert result["step_results"][1]["active_users"] == 500
        assert result["breakdown"] == []

    @pytest.mark.asyncio
    async def test_reserved_total_row_becomes_the_step_result_when_breakdown_requested(self) -> None:
        resp = _funnel_response([
            ("1. Step 1", 1000, 0.5, 500, 0.5, "RESERVED_TOTAL"),
            ("1. Step 1", 700, 0.4, 420, 0.6, "desktop"),
            ("1. Step 1", 300, 0.7, 90, 0.3, "mobile"),
        ])
        steps = [{"label": "Step 1", "match_type": "contains", "pattern": "/a", **_STEP_KWARGS}]
        with patch.object(httpx.AsyncClient, "post", new=AsyncMock(return_value=resp)):
            result = await AnalyticsConnector("token").run_funnel_report(
                "properties/123", steps, "2026-07-01", "2026-07-21", breakdown_dimension="deviceCategory",
            )
        assert len(result["step_results"]) == 1
        assert result["step_results"][0]["active_users"] == 1000
        assert {b["value"]: b["active_users"] for b in result["breakdown"]} == {"desktop": 700, "mobile": 300}

    @pytest.mark.asyncio
    async def test_omitted_trailing_step_reads_as_zero_not_as_the_final_step(self) -> None:
        # Regression: verified against a real GA4 property — when zero
        # users reach a step, GA4 OMITS that step's row entirely rather
        # than returning a zero-value row. Naively reading "the last row
        # GA4 sent back" as the final step would misread this single-row
        # response as "100% conversion" instead of the true 0%.
        resp = _funnel_response([("1. Step 1", 19, 0.0, 19, 1.0, None)])
        with patch.object(httpx.AsyncClient, "post", new=AsyncMock(return_value=resp)):
            result = await AnalyticsConnector("token").run_funnel_report(
                "properties/123", _TWO_STEPS, "2026-07-22", "2026-07-22"
            )
        assert len(result["step_results"]) == 2
        assert result["step_results"][0]["active_users"] == 19
        assert result["step_results"][1] == {
            "step_index": 1, "label": "Step 2", "active_users": 0,
            "completion_rate": 0.0, "abandonments": 0, "abandonment_rate": 0.0,
        }
        assert result["total_entered"] == 19
        assert result["total_completed"] == 0
        assert result["conversion_rate"] == 0.0

    @pytest.mark.asyncio
    async def test_empty_steps_short_circuits_without_a_request(self) -> None:
        mock_post = AsyncMock()
        with patch.object(httpx.AsyncClient, "post", new=mock_post):
            result = await AnalyticsConnector("token").run_funnel_report(
                "properties/123", [], "2026-07-01", "2026-07-21"
            )
        assert result == {
            "step_results": [], "total_entered": 0, "total_completed": 0,
            "conversion_rate": 0.0, "breakdown": [],
        }
        mock_post.assert_not_called()

    @pytest.mark.asyncio
    async def test_zero_entrants_conversion_rate_is_zero_not_a_division_error(self) -> None:
        resp = _funnel_response([("1. Step 1", 0, 0, 0, 0, None)])
        steps = [{"label": "Step 1", "match_type": "contains", "pattern": "/a", **_STEP_KWARGS}]
        with patch.object(httpx.AsyncClient, "post", new=AsyncMock(return_value=resp)):
            result = await AnalyticsConnector("token").run_funnel_report(
                "properties/123", steps, "2026-07-01", "2026-07-21"
            )
        assert result["conversion_rate"] == 0.0

    @pytest.mark.asyncio
    async def test_uses_v1alpha_endpoint_and_closed_funnel(self) -> None:
        resp = _funnel_response([])
        mock_post = AsyncMock(return_value=resp)
        with patch.object(httpx.AsyncClient, "post", new=mock_post):
            await AnalyticsConnector("token").run_funnel_report(
                "properties/123", _TWO_STEPS, "2026-07-01", "2026-07-21"
            )
        called_url = mock_post.call_args.args[0]
        assert called_url == "https://analyticsdata.googleapis.com/v1alpha/properties/123:runFunnelReport"
        body = mock_post.call_args.kwargs["json"]
        assert body["funnel"]["isOpenFunnel"] is False
        assert len(body["funnel"]["steps"]) == 2
        assert "funnelBreakdown" not in body

    @pytest.mark.asyncio
    async def test_breakdown_dimension_included_in_request_when_given(self) -> None:
        resp = _funnel_response([])
        mock_post = AsyncMock(return_value=resp)
        with patch.object(httpx.AsyncClient, "post", new=mock_post):
            await AnalyticsConnector("token").run_funnel_report(
                "properties/123", _TWO_STEPS, "2026-07-01", "2026-07-21", breakdown_dimension="country",
            )
        body = mock_post.call_args.kwargs["json"]
        assert body["funnelBreakdown"] == {"breakdownDimension": {"name": "country"}, "limit": 10}

    @pytest.mark.asyncio
    async def test_bare_property_id_gets_prefixed(self) -> None:
        resp = _funnel_response([])
        mock_post = AsyncMock(return_value=resp)
        with patch.object(httpx.AsyncClient, "post", new=mock_post):
            await AnalyticsConnector("token").run_funnel_report("123456", _TWO_STEPS, "2026-07-01", "2026-07-21")
        called_url = mock_post.call_args.args[0]
        assert called_url.endswith("properties/123456:runFunnelReport")
