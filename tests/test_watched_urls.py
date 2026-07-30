"""Watched URLs — normalization (full URL or bare path/slug -> internal
path + display URL), CSV parsing, and cross-site host validation.

Regression context: the feature must always store/match on the URL PATH
internally, but always DISPLAY the full canonical URL — and a pasted full
URL must be confirmed as belonging to the site before it's trusted (that
same host-matching also closes the SSRF door, since every later fetch
targets site.url + path, never an arbitrary user-supplied host).
"""
import pytest

from app.api.watched_urls import (
    _date_range_list,
    _normalize,
    _parse_csv_urls,
    _path_variants,
    _pick_variant,
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


class TestDateRangeList:
    def test_single_day(self) -> None:
        assert _date_range_list("2026-07-21", "2026-07-21") == ["2026-07-21"]

    def test_multi_day_inclusive_of_both_ends(self) -> None:
        assert _date_range_list("2026-07-01", "2026-07-03") == ["2026-07-01", "2026-07-02", "2026-07-03"]

    def test_seven_day_range_has_seven_dates(self) -> None:
        assert len(_date_range_list("2026-07-01", "2026-07-07")) == 7
