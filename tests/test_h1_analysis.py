"""H1 analysis — body-only counts must be confirmed against the live page.

WordPress's REST API `content.rendered` contains only the post body; themes
render the H1 title in the template around it. A body H1 count of 0 is
therefore NOT evidence of a missing H1 — only a live-page crawl can confirm.
"""
from app.agents.optimizer.content_scorer import _analyze


def _wp_post(body_html: str) -> dict:
    return {
        "id": 1,
        "title": {"rendered": "Test Post"},
        "link": "https://example.com/test-post/",
        "content": {"rendered": body_html},
        "excerpt": {"rendered": "An excerpt long enough to look like a meta description for tests."},
        "date_gmt": "2026-06-01T00:00:00",
        "modified_gmt": "2026-06-20T00:00:00",
    }


BODY_NO_H1 = "<h2>Section</h2>" + "<p>word </p>" * 400
BODY_WITH_H1 = "<h1>Title</h1><h2>Section</h2>" + "<p>word </p>" * 400


def _headings(body: str, live_h1_count: int | None) -> tuple[dict, list[str]]:
    _, issues, breakdown, _, _ = _analyze(
        _wp_post(body), "https://example.com", live_h1_count=live_h1_count
    )
    return breakdown["headings"], issues


class TestH1LiveConfirmation:
    def test_theme_rendered_h1_is_not_flagged(self) -> None:
        # Body has no H1 but the live page does → theme renders it, all good
        headings, issues = _headings(BODY_NO_H1, live_h1_count=1)
        assert headings["h1_count"] == 1
        assert headings["h1_source"] == "live_page"
        assert not any("Missing H1" in i for i in issues)

    def test_confirmed_missing_h1_is_flagged(self) -> None:
        # Live crawl confirmed zero H1s anywhere on the rendered page
        headings, issues = _headings(BODY_NO_H1, live_h1_count=0)
        assert headings["h1_count"] == 0
        assert headings["status"] == "warning"
        assert any("Missing H1" in i for i in issues)

    def test_unverified_zero_is_not_accused(self) -> None:
        # Crawl failed / unavailable (None) — absence of evidence is not evidence
        headings, issues = _headings(BODY_NO_H1, live_h1_count=None)
        assert headings["h1_count"] == 0
        assert headings["h1_source"] == "post_content"
        assert not any("Missing H1" in i for i in issues)

    def test_body_h1_needs_no_confirmation(self) -> None:
        headings, _ = _headings(BODY_WITH_H1, live_h1_count=None)
        assert headings["h1_count"] == 1
        assert headings["h1_source"] == "post_content"

    def test_multiple_h1s_on_live_page_flagged(self) -> None:
        # Theme renders more than one H1 → genuine SEO issue
        headings, issues = _headings(BODY_NO_H1, live_h1_count=3)
        assert headings["h1_count"] == 3
        assert any("Multiple H1" in i for i in issues)
