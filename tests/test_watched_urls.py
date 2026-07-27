"""Watched URLs — normalization (full URL or bare path/slug -> internal
path + display URL), CSV parsing, and cross-site host validation.

Regression context: the feature must always store/match on the URL PATH
internally, but always DISPLAY the full canonical URL — and a pasted full
URL must be confirmed as belonging to the site before it's trusted (that
same host-matching also closes the SSRF door, since every later fetch
targets site.url + path, never an arbitrary user-supplied host).
"""
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app.api.watched_urls import (
    _date_range_list,
    _normalize,
    _parse_csv_urls,
    _path_variants,
    _pick_variant,
    _quarter_start,
    _resolve_date_range,
)

SITE_URL = "https://www.example.com"


class TestNormalize:
    def test_full_url_on_site_domain(self) -> None:
        full, path = _normalize("https://www.example.com/pricing/", SITE_URL)
        assert full == "https://www.example.com/pricing/"
        assert path == "/pricing/"

    def test_bare_path_combined_with_site_base(self) -> None:
        full, path = _normalize("/blog/my-post/", SITE_URL)
        assert full == "https://www.example.com/blog/my-post/"
        assert path == "/blog/my-post/"

    def test_bare_slug_without_leading_slash(self) -> None:
        full, path = _normalize("my-post", SITE_URL)
        assert full == "https://www.example.com/my-post"
        assert path == "/my-post"

    def test_full_url_on_a_different_host_is_rejected(self) -> None:
        with pytest.raises(ValueError, match="not a page on"):
            _normalize("https://evil.example.org/pricing/", SITE_URL)

    def test_empty_input_rejected(self) -> None:
        with pytest.raises(ValueError):
            _normalize("   ", SITE_URL)

    def test_whitespace_is_trimmed(self) -> None:
        full, path = _normalize("  /pricing/  ", SITE_URL)
        assert path == "/pricing/"
        assert full == "https://www.example.com/pricing/"

    def test_site_url_trailing_slash_does_not_double_up(self) -> None:
        full, _ = _normalize("/pricing/", "https://www.example.com/")
        assert full == "https://www.example.com/pricing/"


class TestParseCsvUrls:
    def test_single_column_no_header(self) -> None:
        content = b"/pricing/\n/blog/post-1/\n/blog/post-2/\n"
        assert _parse_csv_urls(content) == ["/pricing/", "/blog/post-1/", "/blog/post-2/"]

    def test_recognized_header_row_is_skipped(self) -> None:
        content = b"URL\n/pricing/\n/about/\n"
        assert _parse_csv_urls(content) == ["/pricing/", "/about/"]

    def test_unrecognized_first_row_is_kept_as_data(self) -> None:
        # Looks like a real path, not a header word — must not be dropped
        content = b"/first-real-path/\n/second/\n"
        assert _parse_csv_urls(content) == ["/first-real-path/", "/second/"]

    def test_url_header_in_first_column(self) -> None:
        content = b"url,notes\n/pricing/,important\n/about/,low priority\n"
        assert _parse_csv_urls(content) == ["/pricing/", "/about/"]

    def test_url_header_found_regardless_of_column_position(self) -> None:
        # The URL column is second here — must still be picked up by name,
        # not assumed to be column 0.
        content = b"notes,URL,owner\nimportant,/pricing/,alice\nlow priority,/about/,bob\n"
        assert _parse_csv_urls(content) == ["/pricing/", "/about/"]

    def test_urls_header_case_insensitive_variants(self) -> None:
        for header in ("URL", "url", "Urls", "URLS", "URLs"):
            content = f"{header}\n/pricing/\n".encode()
            assert _parse_csv_urls(content) == ["/pricing/"], f"failed for header {header!r}"

    def test_header_only_no_data_rows(self) -> None:
        assert _parse_csv_urls(b"URL\n") == []

    def test_blank_lines_skipped(self) -> None:
        content = b"/pricing/\n\n/about/\n"
        assert _parse_csv_urls(content) == ["/pricing/", "/about/"]

    def test_excel_utf8_bom_handled(self) -> None:
        content = b"\xef\xbb\xbfurl\n/pricing/\n"
        assert _parse_csv_urls(content) == ["/pricing/"]

    def test_empty_file_returns_empty_list(self) -> None:
        assert _parse_csv_urls(b"") == []


class TestResolveDateRange:
    """Every preset now resolves to REAL calendar dates (not GA4's "today" /
    "NdaysAgo" relative keywords) — needed to label the Active Users column,
    name exports after actual dates, and enumerate exact days for a
    day-wise breakdown. Expectations are computed from the real clock so
    these don't rot into a flaky hardcoded-date test."""

    def test_today_preset(self) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        assert _resolve_date_range("today", None, None) == (today, today)

    def test_yesterday_preset(self) -> None:
        yesterday = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
        assert _resolve_date_range("yesterday", None, None) == (yesterday, yesterday)

    def test_7d_preset_spans_exactly_7_calendar_days_inclusive_of_today(self) -> None:
        today = datetime.now(timezone.utc).date()
        start, end = _resolve_date_range("7d", None, None)
        assert end == today.isoformat()
        assert start == (today - timedelta(days=6)).isoformat()
        assert (date.fromisoformat(end) - date.fromisoformat(start)).days == 6  # 6 nights = 7 days

    def test_28d_preset_spans_28_days(self) -> None:
        start, end = _resolve_date_range("28d", None, None)
        assert (date.fromisoformat(end) - date.fromisoformat(start)).days == 27

    def test_90d_preset_spans_90_days(self) -> None:
        start, end = _resolve_date_range("90d", None, None)
        assert (date.fromisoformat(end) - date.fromisoformat(start)).days == 89

    def test_qtd_starts_on_the_first_of_the_current_quarter(self) -> None:
        today = datetime.now(timezone.utc).date()
        start, end = _resolve_date_range("qtd", None, None)
        assert end == today.isoformat()
        assert date.fromisoformat(start) == _quarter_start(today)
        assert date.fromisoformat(start).month in (1, 4, 7, 10)
        assert date.fromisoformat(start).day == 1

    def test_ytd_starts_on_january_first(self) -> None:
        today = datetime.now(timezone.utc).date()
        start, end = _resolve_date_range("ytd", None, None)
        assert end == today.isoformat()
        assert start == date(today.year, 1, 1).isoformat()

    def test_custom_range_with_valid_dates(self) -> None:
        assert _resolve_date_range("custom", "2026-01-01", "2026-01-31") == ("2026-01-01", "2026-01-31")

    def test_custom_range_missing_dates_raises_422(self) -> None:
        with pytest.raises(HTTPException) as exc_info:
            _resolve_date_range("custom", None, None)
        assert exc_info.value.status_code == 422

    def test_custom_range_malformed_date_raises_422(self) -> None:
        with pytest.raises(HTTPException) as exc_info:
            _resolve_date_range("custom", "01/01/2026", "2026-01-31")
        assert exc_info.value.status_code == 422

    def test_unknown_range_key_raises_422(self) -> None:
        with pytest.raises(HTTPException) as exc_info:
            _resolve_date_range("last-year", None, None)
        assert exc_info.value.status_code == 422


class TestPathVariants:
    def test_includes_both_trailing_slash_forms(self) -> None:
        assert _path_variants("/pricing/") == {"/pricing/", "/pricing"}

    def test_already_bare_path_still_yields_both_forms(self) -> None:
        assert _path_variants("/pricing") == {"/pricing/", "/pricing"}

    def test_root_path_is_safe(self) -> None:
        # rstrip("/") on "/" alone yields "" — must not produce a bogus "//" variant
        assert _path_variants("/") == {"/", ""}


class TestPickVariant:
    """Regression coverage for a real bug: summing GA4 data across both
    trailing-slash variants of a path double-counted active users and
    pushed bounce rate past 100% (a stray near-instant-bounce hit under the
    bare path was added on top of the real number for the canonical,
    trailing-slash path). Exactly one variant's data must be used, never a
    sum of more than one."""

    def test_prefers_the_exact_canonical_path_when_present(self) -> None:
        by_path = {"/pricing/": 2, "/pricing": 1}
        assert _pick_variant("/pricing/", by_path) == 2

    def test_falls_back_to_another_variant_when_canonical_is_absent(self) -> None:
        by_path = {"/pricing": 5}
        assert _pick_variant("/pricing/", by_path) == 5

    def test_never_sums_multiple_variants(self) -> None:
        # The exact real-world case that produced 200% bounce rate: both
        # variants have data, and only the canonical one must be used.
        by_path = {
            "/dropbox-to-g-suite-migration/": {"avg_engagement_time": 37.0, "bounce_rate": 1.0},
            "/dropbox-to-g-suite-migration": {"avg_engagement_time": 2.0, "bounce_rate": 1.0},
        }
        result = _pick_variant("/dropbox-to-g-suite-migration/", by_path)
        assert result == {"avg_engagement_time": 37.0, "bounce_rate": 1.0}

    def test_returns_none_when_no_variant_has_data(self) -> None:
        assert _pick_variant("/pricing/", {}) is None


class TestQuarterStart:
    def test_q1(self) -> None:
        assert _quarter_start(date(2026, 2, 15)) == date(2026, 1, 1)

    def test_q2(self) -> None:
        assert _quarter_start(date(2026, 5, 20)) == date(2026, 4, 1)

    def test_q3(self) -> None:
        assert _quarter_start(date(2026, 7, 21)) == date(2026, 7, 1)

    def test_q4(self) -> None:
        assert _quarter_start(date(2026, 11, 1)) == date(2026, 10, 1)

    def test_first_day_of_quarter_returns_itself(self) -> None:
        assert _quarter_start(date(2026, 10, 1)) == date(2026, 10, 1)


class TestDateRangeList:
    def test_single_day(self) -> None:
        assert _date_range_list("2026-07-21", "2026-07-21") == ["2026-07-21"]

    def test_multi_day_inclusive_of_both_ends(self) -> None:
        assert _date_range_list("2026-07-01", "2026-07-03") == ["2026-07-01", "2026-07-02", "2026-07-03"]

    def test_seven_day_range_has_seven_dates(self) -> None:
        assert len(_date_range_list("2026-07-01", "2026-07-07")) == 7
