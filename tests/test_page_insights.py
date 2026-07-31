"""Deterministic page insights — the free, instant baseline analysis shown on
every page view, with AI reserved for explicit per-page requests.

Two properties matter most and are easy to get wrong:
  - Ranking. A page leaking hundreds of clicks must outrank a missing meta
    description, or the list buries its own most valuable finding.
  - Availability. Every source is optional; a site with no Analytics, no
    Search Console and no speed test must still get its content findings
    rather than an empty list.
"""
from app.agents.optimizer.insights import build_insights

CTR_OPP = {"position": 7.1, "ctr": 0.44, "typical_ctr": 3.0, "potential_clicks": 238}


def _ids(insights: list[dict]) -> list[str]:
    return [i["id"] for i in insights]


class TestAvailability:
    def test_no_data_at_all_yields_nothing(self) -> None:
        assert build_insights() == []

    def test_content_only_still_produces_findings(self) -> None:
        # The common case: a site with nothing connected yet.
        out = build_insights(breakdown={
            "word_count": {"status": "warning", "detail": "934 words — aim for 1,000+"},
        })
        assert _ids(out) == ["content_word_count"]
        assert out[0]["action"]

    def test_search_only_still_produces_findings(self) -> None:
        out = build_insights(search={"ctr_opportunity": CTR_OPP, "clicks": 35})
        assert _ids(out) == ["ctr_gap"]

    def test_each_source_contributes_independently(self) -> None:
        out = build_insights(
            breakdown={"freshness": {"status": "critical", "detail": "Stale — 443 days"}},
            traffic={"visitors": 100, "bounce_rate": 85.0},
            search={"ctr_opportunity": CTR_OPP, "clicks": 35},
            speed={"score": 45, "failing_vitals": ["CLS"], "visitors": 100},
        )
        assert {i["source"] for i in out} == {"content", "traffic", "search", "speed"}


class TestRanking:
    def test_a_big_click_leak_outranks_a_content_nitpick(self) -> None:
        out = build_insights(
            breakdown={"title": {"status": "warning", "detail": "Title slightly long"}},
            search={"ctr_opportunity": CTR_OPP, "clicks": 35},
        )
        assert _ids(out)[0] == "ctr_gap"

    def test_critical_outranks_warning_regardless_of_impact(self) -> None:
        out = build_insights(
            # Huge impact, but only a warning…
            traffic={"visitors": 100000, "bounce_rate": 75.0},
            # …versus a small-impact critical.
            breakdown={"freshness": {"status": "critical", "detail": "Stale"}},
        )
        assert out[0]["severity"] == "critical"

    def test_within_one_severity_higher_impact_comes_first(self) -> None:
        out = build_insights(search={
            "ctr_opportunity": None,
            "striking_distance": [{"query": "q", "impressions": 5000, "position": 6.0}],
            "position_change": 2.0,
            "clicks": 1,
        })
        assert _ids(out)[0] == "striking_distance"

    def test_a_severe_click_leak_is_escalated_to_critical(self) -> None:
        # 238 potential vs 35 actual = ~203 clicks missing — page-one traffic
        # being thrown away is the most valuable fix available.
        out = build_insights(search={"ctr_opportunity": CTR_OPP, "clicks": 35})
        assert out[0]["severity"] == "critical"

    def test_a_small_click_leak_stays_a_warning(self) -> None:
        out = build_insights(search={
            "ctr_opportunity": {**CTR_OPP, "potential_clicks": 60}, "clicks": 30,
        })
        assert out[0]["severity"] == "warning"


class TestRulesDoNotFireSpuriously:
    def test_healthy_content_categories_produce_no_findings(self) -> None:
        assert build_insights(breakdown={
            "links": {"status": "good", "detail": "6 links"},
            "images": {"status": "good", "detail": "4 images"},
        }) == []

    def test_a_fast_page_with_passing_vitals_is_silent(self) -> None:
        assert build_insights(speed={"score": 100, "failing_vitals": [], "visitors": 50}) == []

    def test_a_high_score_with_a_failing_vital_still_reports(self) -> None:
        # 92/100 looks fine, but a failing vital is a real user-facing problem.
        out = build_insights(speed={"score": 92, "failing_vitals": ["CLS"], "visitors": 50})
        assert _ids(out) == ["pagespeed"]
        assert "CLS" in out[0]["detail"]

    def test_bounce_rule_needs_actual_visitors(self) -> None:
        # A 100% bounce rate on zero traffic is noise, not a finding.
        assert build_insights(traffic={"visitors": 0, "bounce_rate": 100.0}) == []

    def test_no_conversions_rule_needs_meaningful_traffic(self) -> None:
        assert build_insights(traffic={"visitors": 3, "leads": 0}) == []
        out = build_insights(traffic={"visitors": 50, "leads": 0})
        assert "no_conversions" in _ids(out)

    def test_conversions_present_means_no_conversion_finding(self) -> None:
        out = build_insights(traffic={"visitors": 50, "leads": 2})
        assert "no_conversions" not in _ids(out)

    def test_position_improving_is_not_reported_as_slipping(self) -> None:
        # Negative change = moved UP the results.
        out = build_insights(search={"position_change": -1.0, "clicks": 10})
        assert "position_slipped" not in _ids(out)


class TestInsightShape:
    def test_every_insight_carries_an_action_and_evidence(self) -> None:
        out = build_insights(
            breakdown={"word_count": {"status": "warning", "detail": "934 words"}},
            traffic={"visitors": 100, "bounce_rate": 85.0},
            search={"ctr_opportunity": CTR_OPP, "clicks": 35},
            speed={"score": 40, "failing_vitals": ["LCP"], "visitors": 100},
        )
        assert out
        for i in out:
            assert i["action"], f"{i['id']} has no action"
            assert i["evidence"], f"{i['id']} has no evidence"
            assert i["severity"] in ("critical", "warning", "info")

    def test_content_findings_surface_even_with_no_traffic_yet(self) -> None:
        # A brand-new page has no visitors to weight impact by; its content
        # problems must not fall out of the list as a result.
        out = build_insights(
            breakdown={"word_count": {"status": "warning", "detail": "300 words"}},
            traffic={"visitors": 0},
        )
        assert _ids(out) == ["content_word_count"]
        assert out[0]["impact"] >= 1
