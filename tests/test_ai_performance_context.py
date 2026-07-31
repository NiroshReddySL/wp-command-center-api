"""Real measured performance (traffic / Search Console / PageSpeed) folded
into the AI recommendation prompt.

The point is to move advice from "add 200 words" to "you rank 7th for this
exact query and almost nobody clicks — rewrite the title". Every source is
optional and independent, because sites genuinely differ: no GA4, an
unverified Search Console property, a page never speed-tested. The block must
degrade one source at a time without ever inventing a number, since "0 clicks"
and "we don't know" would lead the model to opposite conclusions.
"""
from app.agents.optimizer.content_scorer import _performance_block


class TestPerformanceBlockAvailability:
    def test_no_data_at_all_produces_nothing(self) -> None:
        # Must be byte-identical to the content-only prompt, not an empty
        # header implying we looked and found zero.
        assert _performance_block(None) == ""
        assert _performance_block({}) == ""

    def test_unrecognised_keys_alone_still_produce_nothing(self) -> None:
        assert _performance_block({"something_else": 5}) == ""

    def test_traffic_only(self) -> None:
        block = _performance_block({"visitors_30d": 1234})
        assert "1,234 visitors/30d" in block
        assert "Google search" not in block
        assert "PageSpeed" not in block

    def test_search_only(self) -> None:
        block = _performance_block({
            "search_clicks": 35, "search_impressions": 7937,
            "search_ctr": 0.44, "search_position": 7.1,
        })
        assert "35 clicks" in block
        assert "7,937 impressions" in block
        assert "CTR 0.44%" in block
        assert "avg position 7.1" in block
        assert "Traffic" not in block

    def test_speed_only_with_failing_vitals_named(self) -> None:
        block = _performance_block({
            "speed_score": 62, "speed_strategy": "desktop", "failing_vitals": ["CLS", "TTFB"],
        })
        assert "62/100 (desktop)" in block
        assert "failing: CLS, TTFB" in block

    def test_speed_with_no_failing_vitals_omits_the_failing_clause(self) -> None:
        block = _performance_block({"speed_score": 100, "speed_strategy": "desktop", "failing_vitals": []})
        assert "100/100 (desktop)" in block
        assert "failing" not in block

    def test_all_sources_together(self) -> None:
        block = _performance_block({
            "visitors_30d": 13, "bounce_rate": 70.6, "avg_engagement_time": 10.0,
            "leads": 1,
            "search_clicks": 35, "search_impressions": 7937,
            "search_ctr": 0.44, "search_position": 7.1,
            "speed_score": 100, "speed_strategy": "desktop", "failing_vitals": [],
        })
        for expected in ["13 visitors/30d", "bounce 71%", "1 reached the confirmation page",
                         "35 clicks", "100/100"]:
            assert expected in block, expected


class TestPerformanceBlockInsights:
    def test_ctr_gap_states_the_diagnosis_not_just_the_numbers(self) -> None:
        # The model needs to be told this is a title/meta problem rather than
        # a ranking problem, or it defaults to generic "improve SEO" advice.
        block = _performance_block({
            "ctr_opportunity": {
                "position": 7.1, "ctr": 0.44, "typical_ctr": 3.0, "potential_clicks": 238,
            },
        })
        assert "CTR GAP" in block
        assert "7.1" in block and "0.44%" in block and "3.0%" in block
        assert "title/meta description" in block

    def test_real_queries_are_quoted_verbatim_for_rewriting(self) -> None:
        # Exact wording matters — it's what lets the model align the title
        # with how people actually search.
        block = _performance_block({
            "top_queries": [
                {"query": "can you transfer google calendar to another account",
                 "impressions": 35, "position": 8.6},
            ],
        })
        assert '"can you transfer google calendar to another account"' in block
        assert "35 impressions" in block
        assert "position 8.6" in block

    def test_query_list_is_capped(self) -> None:
        block = _performance_block({
            "top_queries": [
                {"query": f"query {i}", "impressions": 100 - i, "position": 5.0}
                for i in range(12)
            ],
        })
        assert '"query 4"' in block
        assert '"query 5"' not in block  # capped at 5

    def test_block_tells_the_model_to_prefer_measured_data(self) -> None:
        block = _performance_block({"visitors_30d": 10})
        assert "MEASURED PERFORMANCE" in block
        assert "base advice on this" in block


class TestPerformanceBlockNeverInventsNumbers:
    """A missing metric must be an absent line, never a zero — the two mean
    opposite things to the model."""

    def test_missing_metrics_are_omitted_rather_than_zero_filled(self) -> None:
        block = _performance_block({"search_impressions": 500})
        assert "500 impressions" in block
        assert "clicks" not in block
        assert "CTR" not in block
        assert "position" not in block

    def test_a_genuine_zero_is_still_reported(self) -> None:
        # 0 clicks on 500 impressions is real, important data.
        block = _performance_block({"search_clicks": 0, "search_impressions": 500})
        assert "0 clicks" in block

    def test_zero_leads_is_reported_not_dropped(self) -> None:
        block = _performance_block({"leads": 0})
        assert "0 reached the confirmation page" in block
