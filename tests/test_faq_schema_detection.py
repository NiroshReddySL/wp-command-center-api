"""FAQPage schema detection — must not false-flag sites where FAQ schema is
injected outside the REST body / Yoast graph (a separate FAQ plugin/block
rendering straight into the page footer or a widget is the common case).

Regression for: "Flush all & Re-run" flagged "FAQ content detected — add
FAQPage schema" on posts that DO have FAQPage schema, while rescanning the
same post from Content Analysis correctly showed it as present. The batch
run skipped the live full-page crawl whenever ANY schema (e.g. just Yoast's
Article/WebPage) was found locally, so a FAQPage type living elsewhere on
the page was never seen.
"""
from app.agents.optimizer.content_scorer import _analyze, _faq_content_detected, _needs_live_fetch

FAQ_BODY = """
<h1>Post Title</h1>
<h2>Frequently Asked Questions</h2>
<h2>What is included?</h2><p>Everything.</p>
<h2>How do I cancel?</h2><p>Anytime.</p>
<h2>Is support included?</h2><p>Yes.</p>
"""

NON_FAQ_BODY = "<h1>Post Title</h1><p>Just a regular paragraph with no questions at all.</p>"


def _wp_post(body_html: str) -> dict:
    return {
        "id": 1,
        "title": {"rendered": "A sufficiently long and descriptive post title"},
        "content": {"rendered": body_html},
        "link": "https://example.com/post",
        "modified": "2026-06-01T00:00:00",
    }


class TestFaqContentDetected:
    def test_multiple_question_headings_detected(self) -> None:
        assert _faq_content_detected(FAQ_BODY) is True

    def test_plain_content_not_detected(self) -> None:
        assert _faq_content_detected(NON_FAQ_BODY) is False


class TestNeedsLiveFetch:
    def test_no_local_schema_needs_fetch(self) -> None:
        assert _needs_live_fetch([], FAQ_BODY) is True

    def test_faq_body_without_local_faqpage_needs_fetch(self) -> None:
        """The exact regression: Yoast reports Article/WebPage locally (so
        'found' is truthy) but never FAQPage — must still trigger a live
        crawl instead of trusting the local list as complete."""
        assert _needs_live_fetch(["Article", "WebPage"], FAQ_BODY) is True

    def test_faq_body_with_local_faqpage_already_confirmed_skips_fetch(self) -> None:
        assert _needs_live_fetch(["Article", "FAQPage"], FAQ_BODY) is False

    def test_non_faq_body_with_local_schema_skips_fetch(self) -> None:
        assert _needs_live_fetch(["Article", "WebPage"], NON_FAQ_BODY) is False


class TestAnalyzeUsesLiveSchemaWhenProvided:
    def test_faq_flagged_missing_without_live_confirmation(self) -> None:
        """Old behavior when the caller passes only the local (incomplete) types."""
        _, issues, breakdown, _, _ = _analyze(
            _wp_post(FAQ_BODY), "https://example.com", extra_schema_types=["Article", "WebPage"]
        )
        assert breakdown["schema_markup"]["faq_recommendation"] == "missing"
        assert any("FAQPage schema" in i for i in issues)

    def test_faq_not_flagged_when_live_crawl_finds_faqpage(self) -> None:
        """The fix: once the caller passes the live full-page schema types
        (which include FAQPage found elsewhere on the page), it must be
        recognized as present and NOT flagged as a missing-schema issue."""
        _, issues, breakdown, _, _ = _analyze(
            _wp_post(FAQ_BODY), "https://example.com",
            extra_schema_types=["Article", "WebPage", "FAQPage"],
        )
        assert breakdown["schema_markup"]["has_faq_schema"] is True
        assert breakdown["schema_markup"]["faq_recommendation"] == "present"
        assert not any("FAQPage schema" in i for i in issues)
