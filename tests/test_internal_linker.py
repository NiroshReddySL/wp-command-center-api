"""Internal Linker relevance scoring — rewritten because raw shared-keyword
counting treated a site-wide term (e.g. "migration" on a migration company's
blog) the same as a genuinely rare, specific shared term. Weighting by IDF
(how many OTHER posts also use a word) fixes that without a hand-maintained
per-site stop list, and pairs are now ranked so the verification pass spends
its budget on the strongest matches instead of whichever were found first.

Anchor-text selection was rewritten a second time after a real, reported bug:
the first rewrite picked anchor text from the TARGET page's top Search
Console query with no check that the phrase had anything to do with the
SOURCE page. That produced an actually-broken suggestion — the anchor
"can we send mail from gmail to outlook" inserted into an article titled
"Export Outlook Emails to Gmail" (the opposite direction), a phrase that
never appeared anywhere in that article's real text. `_resolve_link` now
requires an anchor to be verifiably present in the chosen source's actual
WordPress content before it's ever suggested, trying both link directions
and dropping the pair entirely if neither has one.
"""
from app.agents.optimizer.internal_linker import (
    _MAX_QUERY_WORDS,
    _MIN_PAIR_SCORE,
    _MIN_QUERY_IMPRESSIONS,
    _choose_direction,
    _extract_anchor_from_text,
    _extract_text,
    _idf_weights,
    _keywords,
    _pair_score,
    _phrase_in_text,
    _pick_query_anchor,
    _resolve_link,
    _sentences,
    _trim_weak_edges,
)


class _FakePost:
    """Minimal ContentPost stand-in — _resolve_link only reads .id and .url."""

    def __init__(self, id: str, url: str) -> None:
        self.id = id
        self.url = url


class TestKeywords:
    def test_extracts_words_of_four_or_more_letters(self) -> None:
        assert _keywords("Cloud Migration Guide") == {"cloud", "migration", "guide"}

    def test_drops_stop_words(self) -> None:
        assert "with" not in _keywords("Migration with Your Team")
        assert "your" not in _keywords("Migration with Your Team")

    def test_drops_short_words(self) -> None:
        assert "aws" not in _keywords("AWS Migration Tips")  # 3 letters


class TestIdfWeights:
    def test_word_in_every_post_gets_the_minimum_weight(self) -> None:
        posts = {
            "1": {"migration", "cloud"},
            "2": {"migration", "data"},
            "3": {"migration", "backup"},
        }
        idf = _idf_weights(posts)
        # "migration" appears in all 3 posts — should score the lowest of any word here.
        assert idf["migration"] == min(idf.values())

    def test_rare_word_outweighs_a_word_shared_by_every_post(self) -> None:
        posts = {
            "1": {"migration", "sharepoint"},
            "2": {"migration", "cloud"},
            "3": {"migration", "backup"},
            "4": {"migration", "compliance"},
        }
        idf = _idf_weights(posts)
        assert idf["sharepoint"] > idf["migration"]

    def test_smoothed_formula_is_always_at_least_one(self) -> None:
        # Even a word present in literally every post must score >= 1, so a
        # threshold like _MIN_PAIR_SCORE can meaningfully reject "common word only" pairs.
        posts = {"1": {"migration"}, "2": {"migration"}}
        idf = _idf_weights(posts)
        assert idf["migration"] >= 1.0


class TestPairScore:
    def test_sums_idf_of_shared_words(self) -> None:
        idf = {"migration": 1.2, "sharepoint": 4.5}
        assert _pair_score({"migration", "sharepoint"}, idf) == 1.2 + 4.5

    def test_unknown_word_defaults_to_one(self) -> None:
        assert _pair_score({"unseen"}, {}) == 1.0

    def test_two_generic_words_can_fall_below_the_threshold(self) -> None:
        # Both words near the IDF floor (~1.0 each) — a site-wide-topic-only
        # match, which is exactly what the rewrite is meant to filter out.
        idf = {"migration": 1.1, "cloud": 1.15}
        assert _pair_score({"migration", "cloud"}, idf) < _MIN_PAIR_SCORE

    def test_one_specific_word_can_clear_the_threshold_alone(self) -> None:
        idf = {"sharepoint": 4.5}
        assert _pair_score({"sharepoint"}, idf) >= _MIN_PAIR_SCORE


class TestExtractText:
    def test_strips_html_tags(self) -> None:
        assert _extract_text("<p>Migrate <strong>files</strong> now.</p>") == "Migrate files now."

    def test_decodes_html_entities(self) -> None:
        # This is the exact bug class that broke matching before: raw
        # "&#038;" never equals a clean "&" when compared to a candidate phrase.
        assert _extract_text("Inbox, Sent &#038; Drafts") == "Inbox, Sent & Drafts"

    def test_collapses_whitespace(self) -> None:
        assert _extract_text("<p>Line one</p>\n\n<p>Line   two</p>") == "Line one Line two"

    def test_empty_or_none_input_is_safe(self) -> None:
        assert _extract_text("") == ""
        assert _extract_text(None) == ""  # type: ignore[arg-type]


class TestPhraseInText:
    def test_finds_exact_phrase_case_insensitively(self) -> None:
        assert _phrase_in_text("Export Outlook Emails", "how to export outlook emails to gmail")

    def test_rejects_phrase_from_the_opposite_direction(self) -> None:
        # The actual reported bug: a "Gmail to Outlook" query must not match
        # an "Outlook to Gmail" article's text just because words overlap.
        source_text = "this guide covers how to export outlook emails to gmail including inbox sent and drafts"
        assert not _phrase_in_text("can we send mail from gmail to outlook", source_text)

    def test_empty_phrase_or_text_is_false(self) -> None:
        assert not _phrase_in_text("", "some text")
        assert not _phrase_in_text("phrase", "")


class TestExtractAnchorFromText:
    def test_extracts_a_window_around_the_first_matching_keyword(self) -> None:
        text = "This complete guide explains how to export outlook emails to gmail safely"
        result = _extract_anchor_from_text(text, ["outlook", "gmail"])
        assert result is not None
        assert "outlook" in result.lower()

    def test_tries_keywords_in_ranked_order(self) -> None:
        text = "A guide about sharepoint migration and general cloud storage topics"
        # "sharepoint" isn't present; "migration" is — should fall through to it.
        result = _extract_anchor_from_text(text, ["nonexistent", "migration"])
        assert result is not None
        assert "migration" in result.lower()

    def test_returns_none_when_no_keyword_is_present(self) -> None:
        text = "A totally unrelated article about office chairs"
        assert _extract_anchor_from_text(text, ["sharepoint", "migration"]) is None

    def test_returns_none_for_empty_source_text(self) -> None:
        assert _extract_anchor_from_text("", ["migration"]) is None

    def test_never_spans_two_sentences(self) -> None:
        # "migration" sits right at the end of its sentence — the window
        # must not pull words from the unrelated sentence that follows.
        text = "This page is all about sharepoint migration. Office chairs are also nice."
        result = _extract_anchor_from_text(text, ["migration"])
        assert result is not None
        assert "chairs" not in result.lower()

    def test_does_not_start_or_end_on_a_dangling_preposition(self) -> None:
        text = "Filter Matched Users to Streamline Slack Migration to Teams smoothly"
        result = _extract_anchor_from_text(text, ["matched"], window=3)
        assert result is not None
        first, last = result.lower().split()[0], result.lower().split()[-1]
        assert first not in {"to", "a", "an", "the", "for", "of", "in", "on", "and", "or", "with", "from", "by", "as"}
        assert last not in {"to", "a", "an", "the", "for", "of", "in", "on", "and", "or", "with", "from", "by", "as"}


class TestSentences:
    def test_splits_on_sentence_terminators(self) -> None:
        assert _sentences("First one. Second one! Third one?") == ["First one.", "Second one!", "Third one?"]

    def test_ignores_empty_fragments(self) -> None:
        assert _sentences("Only one sentence.") == ["Only one sentence."]


class TestTrimWeakEdges:
    def test_trims_a_trailing_preposition(self) -> None:
        assert _trim_weak_edges("Filter Matched Users to") == "Filter Matched Users"

    def test_trims_a_leading_article(self) -> None:
        assert _trim_weak_edges("The Migration Guide") == "Migration Guide"

    def test_never_trims_below_two_words(self) -> None:
        assert _trim_weak_edges("to Migrate") == "to Migrate"

    def test_leaves_a_clean_phrase_untouched(self) -> None:
        assert _trim_weak_edges("Sharepoint Migration Guide") == "Sharepoint Migration Guide"


class TestPickQueryAnchor:
    def test_picks_highest_clicks_among_qualifying_queries(self) -> None:
        queries = [
            {"query": "migrate dropbox to sharepoint", "clicks": 12, "impressions": 100, "position": 3.0},
            {"query": "sharepoint migration guide", "clicks": 40, "impressions": 90, "position": 5.0},
        ]
        result = _pick_query_anchor(queries)
        assert result["query"] == "sharepoint migration guide"

    def test_rejects_single_word_queries(self) -> None:
        queries = [{"query": "sharepoint", "clicks": 50, "impressions": 500, "position": 1.0}]
        assert _pick_query_anchor(queries) is None

    def test_rejects_queries_below_the_impression_floor(self) -> None:
        queries = [{
            "query": "migrate dropbox to sharepoint", "clicks": 1,
            "impressions": _MIN_QUERY_IMPRESSIONS - 1, "position": 3.0,
        }]
        assert _pick_query_anchor(queries) is None

    def test_rejects_overly_long_queries(self) -> None:
        long_query = " ".join(["word"] * (_MAX_QUERY_WORDS + 1))
        queries = [{"query": long_query, "clicks": 10, "impressions": 100, "position": 3.0}]
        assert _pick_query_anchor(queries) is None

    def test_empty_query_list_returns_none(self) -> None:
        assert _pick_query_anchor([]) is None

    def test_falls_back_to_impressions_when_clicks_tie(self) -> None:
        queries = [
            {"query": "migrate box to sharepoint", "clicks": 5, "impressions": 50, "position": 4.0},
            {"query": "sharepoint migration steps", "clicks": 5, "impressions": 200, "position": 2.0},
        ]
        result = _pick_query_anchor(queries)
        assert result["query"] == "sharepoint migration steps"


class TestResolveLink:
    def test_reproduces_and_fixes_the_reported_bug(self) -> None:
        """The exact real-world scenario that was broken: source is an
        "Outlook to Gmail" article, target is the reverse "Gmail to Outlook"
        guide, and the target's top query is phrased for the reverse
        direction. The query must be rejected (it doesn't appear in the
        source's text) and a same-direction anchor extracted from the
        source's own words used instead."""
        outlook_to_gmail = _FakePost("a", "https://example.com/export-outlook-to-gmail/")
        gmail_to_outlook = _FakePost("b", "https://example.com/export-gmail-to-outlook/")
        source_texts = {
            "a": "how to export outlook emails to gmail including inbox sent and drafts",
            "b": "how to export gmail emails to outlook including inbox sent and drafts",
        }
        queries_by_target_url = {
            gmail_to_outlook.url: [
                {"query": "can we send mail from gmail to outlook", "clicks": 0, "impressions": 12, "position": 8.0},
            ],
        }
        shared = ["drafts", "sent", "inbox", "outlook", "gmail", "export", "emails"]

        result = _resolve_link(outlook_to_gmail, gmail_to_outlook, shared, source_texts, queries_by_target_url)

        assert result is not None
        source, target, anchor, anchor_source, query_match = result
        assert source is outlook_to_gmail
        assert target is gmail_to_outlook  # direction preserved, not flipped
        assert anchor_source == "source_text"
        assert query_match is None
        assert _phrase_in_text(anchor, source_texts["a"])
        assert "gmail to outlook" not in anchor.lower()

    def test_uses_the_search_query_when_it_really_is_present_in_source(self) -> None:
        source = _FakePost("a", "https://example.com/source/")
        target = _FakePost("b", "https://example.com/target/")
        source_texts = {"a": "this article also explains sharepoint migration steps in detail"}
        queries_by_target_url = {
            target.url: [{"query": "sharepoint migration steps", "clicks": 5, "impressions": 50, "position": 3.0}],
        }

        result = _resolve_link(source, target, ["sharepoint", "migration"], source_texts, queries_by_target_url)

        assert result is not None
        _, _, anchor, anchor_source, query_match = result
        assert anchor_source == "search_query"
        assert anchor == "sharepoint migration steps"
        assert query_match is not None

    def test_flips_direction_when_the_preferred_source_has_no_verifiable_anchor(self) -> None:
        post_x = _FakePost("x", "https://example.com/x/")
        post_y = _FakePost("y", "https://example.com/y/")
        # x's text has nothing to do with the shared keywords; y's does.
        source_texts = {
            "x": "a totally unrelated announcement about office hours",
            "y": "a guide covering sharepoint migration end to end",
        }
        result = _resolve_link(post_x, post_y, ["sharepoint", "migration"], source_texts, {})
        assert result is not None
        source, target, anchor, anchor_source, _ = result
        assert source is post_y
        assert target is post_x
        # No queries were supplied, so the anchor can only have come from y's text.
        assert anchor_source == "source_text"
        assert "migration" in anchor.lower() or "sharepoint" in anchor.lower()

    def test_returns_none_when_neither_direction_has_a_verifiable_anchor(self) -> None:
        post_x = _FakePost("x", "https://example.com/x/")
        post_y = _FakePost("y", "https://example.com/y/")
        source_texts = {"x": "nothing relevant here", "y": "nor here either"}
        assert _resolve_link(post_x, post_y, ["sharepoint", "migration"], source_texts, {}) is None

    def test_returns_none_when_no_source_text_was_fetched_for_either_post(self) -> None:
        post_x = _FakePost("x", "https://example.com/x/")
        post_y = _FakePost("y", "https://example.com/y/")
        assert _resolve_link(post_x, post_y, ["sharepoint"], {}, {}) is None


class TestChooseDirection:
    def test_links_from_higher_traffic_to_lower_traffic(self) -> None:
        strong = ("strong-post", 5000, 80)
        weak = ("weak-post", 100, 80)
        source, target = _choose_direction(strong, weak)
        assert source == strong
        assert target == weak

    def test_order_of_arguments_does_not_matter(self) -> None:
        strong = ("strong-post", 5000, 80)
        weak = ("weak-post", 100, 80)
        source, target = _choose_direction(weak, strong)
        assert source == strong
        assert target == weak

    def test_falls_back_to_health_score_when_traffic_ties(self) -> None:
        healthier = ("a", 0, 90)
        weaker = ("b", 0, 40)
        source, target = _choose_direction(healthier, weaker)
        assert source == healthier
        assert target == weaker

    def test_falls_back_to_id_for_a_fully_deterministic_tie(self) -> None:
        post_a = ("a", 0, 50)
        post_b = ("b", 0, 50)
        source, target = _choose_direction(post_b, post_a)
        assert source == post_a
        assert target == post_b
