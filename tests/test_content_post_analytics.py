"""Content Analysis page's Analytics Overview — added so a per-post detail
page can show real 30-day traffic, bounce rate, and how many visitors from
that post reached the site's own Contact/Pricing page via a real GA4
funnel. Initially shipped as a "page reached" proxy since no form-submission
tracking existed; extended once the site's actual form setup was confirmed
to redirect to a shared confirmation ("thank you") page on success — adding
that page as an optional 3rd funnel step turns "reached" into a real,
attributable "submitted" conversion count.
"""
import re

from app.api.optimizer import (
    _detect_confirmation_page,
    _detect_conversion_targets,
    _fill_daily_gaps,
    _page_location_regex,
    _ctr_opportunity,
    _device_shares,
    _pct_change,
    _rate_metric,
    _rate_speed_score,
    _striking_distance,
    _typical_ctr,
)


class TestTypicalCtr:
    def test_higher_positions_earn_more(self) -> None:
        assert _typical_ctr(1.0) > _typical_ctr(3.0) > _typical_ctr(8.0)

    def test_fractional_positions_round_up_to_the_next_band(self) -> None:
        # An average position of 6.4 is treated as the position-7 band, not
        # position 6 — never flattering the page.
        assert _typical_ctr(6.4) == _typical_ctr(7.0)

    def test_beyond_page_one_has_no_meaningful_benchmark(self) -> None:
        # Past ~10 the rates flatten into noise; returning a number there
        # would be false precision.
        assert _typical_ctr(11.0) is None
        assert _typical_ctr(50.0) is None

    def test_invalid_position_has_no_benchmark(self) -> None:
        assert _typical_ctr(0.0) is None


class TestCtrOpportunity:
    """Flags "ranks well but nobody clicks" — a title/meta problem. Must not
    fire on thin data or on normal variance, or it becomes noise people
    learn to ignore."""

    def test_flags_a_page_ranking_well_with_far_too_few_clicks(self) -> None:
        # The real case that motivated this: position 7.1, 0.44% CTR.
        opp = _ctr_opportunity(position=7.1, ctr=0.44, impressions=7937)
        assert opp is not None
        assert opp["typical_ctr"] == 3.0
        assert opp["potential_clicks"] == int(7937 * 3.0 / 100)

    def test_silent_when_ctr_is_merely_a_little_low(self) -> None:
        # 2.0% against a 3.0% benchmark is within normal variance.
        assert _ctr_opportunity(position=7.0, ctr=2.0, impressions=5000) is None

    def test_silent_on_too_few_impressions_to_judge(self) -> None:
        assert _ctr_opportunity(position=7.0, ctr=0.0, impressions=50) is None

    def test_silent_beyond_page_one_where_low_ctr_is_expected(self) -> None:
        assert _ctr_opportunity(position=25.0, ctr=0.1, impressions=9000) is None

    def test_boundary_at_exactly_half_the_typical_rate(self) -> None:
        # Position 7.0 sits in the 3.5% band, so the trigger is below 1.75%.
        # Exactly half is "not far enough short" — the shortfall must be strict.
        assert _typical_ctr(7.0) == 3.5
        assert _ctr_opportunity(position=7.0, ctr=1.75, impressions=5000) is None
        assert _ctr_opportunity(position=7.0, ctr=1.74, impressions=5000) is not None


class TestStrikingDistance:
    def test_keeps_queries_just_outside_the_click_earning_positions(self) -> None:
        rows = _striking_distance([
            {"query": "a", "position": 4.0, "impressions": 35},
            {"query": "b", "position": 8.6, "impressions": 20},
            {"query": "c", "position": 15.0, "impressions": 12},
        ])
        assert [r["query"] for r in rows] == ["a", "b", "c"]

    def test_excludes_queries_already_winning(self) -> None:
        # Position 1-3 already gets the clicks — no headroom worth chasing.
        assert _striking_distance([{"query": "top", "position": 2.0, "impressions": 500}]) == []

    def test_excludes_queries_too_far_back_to_reach(self) -> None:
        assert _striking_distance([{"query": "far", "position": 40.0, "impressions": 500}]) == []

    def test_excludes_queries_with_too_few_impressions_to_matter(self) -> None:
        assert _striking_distance([{"query": "rare", "position": 6.0, "impressions": 3}]) == []


class TestDeviceShares:
    def test_percentages_and_fixed_category_order(self) -> None:
        # Mobile has the largest share, but desktop must still come first —
        # a colour that moves between categories when the ranking changes
        # would mislead anyone who learned "blue = desktop".
        rows = _device_shares({"mobile": 60, "desktop": 30, "tablet": 10})
        assert [r["device"] for r in rows] == ["desktop", "mobile", "tablet"]
        assert [r["pct"] for r in rows] == [30.0, 60.0, 10.0]

    def test_absent_categories_are_omitted_not_zero_filled(self) -> None:
        rows = _device_shares({"desktop": 5})
        assert [r["device"] for r in rows] == ["desktop"]
        assert rows[0]["pct"] == 100.0

    def test_unknown_categories_fold_into_other_rather_than_vanishing(self) -> None:
        # GA4 occasionally reports "smart tv". Dropping it would make the
        # remaining percentages describe a whole that doesn't exist.
        rows = _device_shares({"desktop": 50, "mobile": 30, "smart tv": 20})
        assert [r["device"] for r in rows] == ["desktop", "mobile", "other"]
        assert rows[-1]["users"] == 20
        assert sum(r["pct"] for r in rows) == 100.0

    def test_percentages_sum_to_100_across_the_real_total(self) -> None:
        rows = _device_shares({"desktop": 1, "mobile": 1, "tablet": 1})
        assert abs(sum(r["pct"] for r in rows) - 100.0) < 0.5

    def test_no_traffic_yields_no_rows_instead_of_dividing_by_zero(self) -> None:
        assert _device_shares({}) == []
        assert _device_shares({"desktop": 0, "mobile": 0}) == []


class TestRateMetric:
    """Google's published Core Web Vitals boundaries. Pinned exactly because
    a page sitting right on a threshold is the case people argue about, and
    because these numbers are Google's to define — not ours to drift."""

    def test_lcp_boundaries(self) -> None:
        assert _rate_metric("lcp", 2500.0) == "good"        # inclusive
        assert _rate_metric("lcp", 2500.1) == "needs_work"
        assert _rate_metric("lcp", 4000.0) == "needs_work"  # inclusive
        assert _rate_metric("lcp", 4000.1) == "poor"

    def test_cls_boundaries(self) -> None:
        assert _rate_metric("cls", 0.1) == "good"
        assert _rate_metric("cls", 0.11) == "needs_work"
        assert _rate_metric("cls", 0.25) == "needs_work"
        assert _rate_metric("cls", 0.26) == "poor"

    def test_tbt_boundaries(self) -> None:
        assert _rate_metric("tbt", 200.0) == "good"
        assert _rate_metric("tbt", 600.0) == "needs_work"
        assert _rate_metric("tbt", 600.1) == "poor"

    def test_ttfb_boundaries(self) -> None:
        assert _rate_metric("ttfb", 800.0) == "good"
        assert _rate_metric("ttfb", 1800.0) == "needs_work"
        assert _rate_metric("ttfb", 1800.1) == "poor"

    def test_zero_is_good_not_missing(self) -> None:
        # A genuine 0 (e.g. no layout shift at all) is the best possible
        # result and must not be confused with "not measured".
        assert _rate_metric("cls", 0.0) == "good"

    def test_unmeasured_metric_has_no_rating(self) -> None:
        assert _rate_metric("lcp", None) is None

    def test_unknown_metric_has_no_rating(self) -> None:
        assert _rate_metric("not_a_vital", 123.0) is None


class TestRateSpeedScore:
    """The Lighthouse score runs the opposite direction to the timing
    metrics — higher is better — which is exactly the kind of thing that
    gets inverted by accident."""

    def test_score_bands(self) -> None:
        assert _rate_speed_score(100) == "good"
        assert _rate_speed_score(90) == "good"    # inclusive
        assert _rate_speed_score(89) == "needs_work"
        assert _rate_speed_score(50) == "needs_work"  # inclusive
        assert _rate_speed_score(49) == "poor"
        assert _rate_speed_score(0) == "poor"

    def test_missing_score_has_no_rating(self) -> None:
        assert _rate_speed_score(None) is None


class TestPageLocationRegex:
    """Regression: the funnel matched page_location with CONTAINS, so a
    malformed URL that merely contained the post path counted as a read of
    that post. On a real site "/transfer-calendar-from-gmail-to-gmail/>"
    (broken href markup) added 2 phantom readers, pushing the funnel's entry
    count above the traffic tile — the reported discrepancy.

    GA4's FULL_REGEXP must match the ENTIRE value, so these assert against
    fullmatch, exactly as GA4 evaluates it.
    """

    PATH = "/transfer-calendar-from-gmail-to-gmail/"

    def _matches(self, url: str) -> bool:
        return re.fullmatch(_page_location_regex(self.PATH), url) is not None

    def test_matches_the_canonical_url(self) -> None:
        assert self._matches("https://www.cloudfuze.com/transfer-calendar-from-gmail-to-gmail/")

    def test_matches_without_a_trailing_slash(self) -> None:
        assert self._matches("https://www.cloudfuze.com/transfer-calendar-from-gmail-to-gmail")

    def test_still_matches_genuine_query_strings(self) -> None:
        # Campaign traffic is real readership and must keep counting.
        assert self._matches(
            "https://www.cloudfuze.com/transfer-calendar-from-gmail-to-gmail/?utm_source=newsletter"
        )

    def test_still_matches_a_fragment(self) -> None:
        assert self._matches("https://www.cloudfuze.com/transfer-calendar-from-gmail-to-gmail/#step-2")

    def test_rejects_the_malformed_broken_markup_url(self) -> None:
        assert not self._matches("https://www.cloudfuze.com/transfer-calendar-from-gmail-to-gmail/>")

    def test_rejects_a_longer_path_that_merely_starts_the_same(self) -> None:
        assert not self._matches(
            "https://www.cloudfuze.com/transfer-calendar-from-gmail-to-gmail-guide/"
        )

    def test_rejects_the_path_appearing_mid_url(self) -> None:
        assert not self._matches(
            "https://www.cloudfuze.com/blog/transfer-calendar-from-gmail-to-gmail/extra/"
        )

    def test_regex_special_characters_in_a_path_are_escaped(self) -> None:
        # A literal "." must not become "any character".
        pattern = _page_location_regex("/pricing.html")
        assert re.fullmatch(pattern, "https://x.com/pricing.html")
        assert not re.fullmatch(pattern, "https://x.com/pricingXhtml")


class TestDetectConversionTargets:
    def test_finds_a_contact_page_by_slug(self) -> None:
        posts = [
            ("1", "Contact Us", "https://example.com/contact/"),
            ("2", "Some Blog Post", "https://example.com/blog/some-post/"),
        ]
        result = _detect_conversion_targets(posts, exclude_id="2")
        assert result["Contact"] == ("1", "Contact Us", "https://example.com/contact/")

    def test_finds_a_pricing_page_by_slug(self) -> None:
        posts = [
            ("1", "Our Pricing", "https://example.com/pricing/"),
            ("2", "Some Blog Post", "https://example.com/blog/some-post/"),
        ]
        result = _detect_conversion_targets(posts, exclude_id="2")
        assert result["Pricing"] == ("1", "Our Pricing", "https://example.com/pricing/")

    def test_prefers_the_shortest_matching_path(self) -> None:
        posts = [
            ("1", "Contact Us", "https://example.com/contact/"),
            ("2", "Contact us for a free quote guide", "https://example.com/contact-us-for-a-free-quote-guide/"),
            ("3", "Some Blog Post", "https://example.com/blog/some-post/"),
        ]
        result = _detect_conversion_targets(posts, exclude_id="3")
        assert result["Contact"][0] == "1"

    def test_never_matches_the_excluded_post_against_itself(self) -> None:
        posts = [("1", "Contact Us", "https://example.com/contact/")]
        result = _detect_conversion_targets(posts, exclude_id="1")
        assert "Contact" not in result

    def test_returns_empty_when_no_candidate_pages_exist(self) -> None:
        posts = [("1", "Some Blog Post", "https://example.com/blog/some-post/")]
        result = _detect_conversion_targets(posts, exclude_id="2")
        assert result == {}

    def test_matches_are_case_insensitive(self) -> None:
        posts = [("1", "Contact", "https://example.com/CONTACT/")]
        result = _detect_conversion_targets(posts, exclude_id="2")
        assert "Contact" in result


class TestDetectConfirmationPage:
    def test_finds_a_thanks_page(self) -> None:
        posts = [
            ("1", "Thank You", "https://example.com/thanks/"),
            ("2", "Some Blog Post", "https://example.com/blog/some-post/"),
        ]
        assert _detect_confirmation_page(posts) == ("1", "Thank You", "https://example.com/thanks/")

    def test_finds_a_hyphenated_thank_you_page(self) -> None:
        posts = [("1", "Thank You", "https://example.com/thank-you/")]
        assert _detect_confirmation_page(posts) is not None

    def test_prefers_the_shortest_matching_path(self) -> None:
        posts = [
            ("1", "Thanks", "https://example.com/thanks/"),
            ("2", "Thanks for contacting us about our services", "https://example.com/thanks-for-contacting-us-about-our-services/"),
        ]
        result = _detect_confirmation_page(posts)
        assert result is not None and result[0] == "1"

    def test_returns_none_when_no_confirmation_page_exists(self) -> None:
        posts = [("1", "Some Blog Post", "https://example.com/blog/some-post/")]
        assert _detect_confirmation_page(posts) is None

    def test_returns_none_for_an_empty_site(self) -> None:
        assert _detect_confirmation_page([]) is None


class TestPctChange:
    def test_computes_a_positive_change(self) -> None:
        assert _pct_change(100, 150) == 50.0

    def test_computes_a_negative_change(self) -> None:
        assert _pct_change(100, 50) == -50.0

    def test_zero_baseline_returns_none_not_a_fake_percentage(self) -> None:
        assert _pct_change(0, 50) is None

    def test_zero_to_zero_returns_none(self) -> None:
        assert _pct_change(0, 0) is None

    def test_no_change_is_zero(self) -> None:
        assert _pct_change(100, 100) == 0.0


class TestFillDailyGaps:
    def test_fills_every_calendar_day_in_range(self) -> None:
        result = _fill_daily_gaps({}, "2026-01-01", "2026-01-03")
        assert result == [
            {"date": "2026-01-01", "views": 0},
            {"date": "2026-01-02", "views": 0},
            {"date": "2026-01-03", "views": 0},
        ]

    def test_uses_real_counts_where_present(self) -> None:
        result = _fill_daily_gaps({"2026-01-02": 42}, "2026-01-01", "2026-01-03")
        assert result[1] == {"date": "2026-01-02", "views": 42}

    def test_single_day_range(self) -> None:
        result = _fill_daily_gaps({"2026-01-01": 5}, "2026-01-01", "2026-01-01")
        assert result == [{"date": "2026-01-01", "views": 5}]

    def test_ignores_counts_outside_the_requested_range(self) -> None:
        result = _fill_daily_gaps({"2025-12-31": 99}, "2026-01-01", "2026-01-01")
        assert result == [{"date": "2026-01-01", "views": 0}]
