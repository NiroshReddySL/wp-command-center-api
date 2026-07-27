"""Slug derivation for clean content-detail URLs."""
from app.api.optimizer import _slug_from_url


class TestSlugFromUrl:
    def test_standard_post_url(self) -> None:
        assert _slug_from_url("https://www.cloudfuze.com/manage-google-workspace-access/") == "manage-google-workspace-access"

    def test_no_trailing_slash(self) -> None:
        assert _slug_from_url("https://site.com/blog/my-post") == "my-post"

    def test_nested_path_takes_last_segment(self) -> None:
        assert _slug_from_url("https://site.com/2026/07/deep-nested-post/") == "deep-nested-post"

    def test_bare_domain_has_no_slug(self) -> None:
        assert _slug_from_url("https://site.com/") is None
        assert _slug_from_url("https://site.com") is None

    def test_empty_and_none_safe(self) -> None:
        assert _slug_from_url("") is None
