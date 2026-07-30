"""Content Analysis page's Analytics Overview — added so a per-post detail
page can show real 30-day traffic, bounce rate, and how many visitors from
that post reached the site's own Contact/Pricing page via a real GA4
funnel. Initially shipped as a "page reached" proxy since no form-submission
tracking existed; extended once the site's actual form setup was confirmed
to redirect to a shared confirmation ("thank you") page on success — adding
that page as an optional 3rd funnel step turns "reached" into a real,
attributable "submitted" conversion count.
"""
from app.api.optimizer import (
    _detect_confirmation_page,
    _detect_conversion_targets,
    _fill_daily_gaps,
    _pct_change,
)


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
