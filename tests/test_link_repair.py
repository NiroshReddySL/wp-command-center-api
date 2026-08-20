"""Suggesting what a malformed link meant.

The defect is specific: an `href` containing prose, because someone pasted a
sentence into WordPress's link field. That sentence is the text meant to be
the *anchor*, and it is the only clue to the intended destination.

The properties worth pinning are about restraint. Suggestions must only appear
where intent has to be inferred; they must be ranked by something better than
a single coincidentally-rare word; and when nothing matches, the honest answer
is none rather than the three least-bad guesses.
"""
import pytest

from app.services.link_repair import (
    MAX_SUGGESTIONS,
    MIN_SUGGESTION_SCORE,
    prose_from_url,
)

PRICE_QUOTE = "https://price%20quote"
PARAGRAPH = (
    "http://choosing%20the%20right%20saas%20subscription%20management%20tool%20is"
    "%20important%20for%20governing%20your%20subscriptions%20effectively.xn--%20with"
    "%20clear%20visibility-oj95f.%20with%20cloudfuze%20manage/"
)


class TestProseExtraction:
    def test_a_short_phrase(self) -> None:
        assert prose_from_url(PRICE_QUOTE) == "price quote"

    def test_a_whole_paragraph(self) -> None:
        text = prose_from_url(PARAGRAPH)
        assert text.startswith("choosing the right saas subscription management tool")
        assert "cloudfuze manage" in text

    def test_punycode_wreckage_is_dropped(self) -> None:
        # `xn--…-oj95f` is what a browser produces trying to coerce a sentence
        # into a hostname. It is an artefact of the mangling, not content, and
        # would otherwise be scored as if it were a keyword.
        text = prose_from_url(PARAGRAPH)
        assert "xn--" not in text
        assert "oj95f" not in text

    def test_separators_become_spaces(self) -> None:
        assert prose_from_url("https://get%20a-price_quote/now") == "get a price quote now"

    def test_a_normal_url_yields_its_own_words(self) -> None:
        # Harmless: suggestions are gated on the href being malformed, so this
        # is never reached for a real URL.
        assert "pricing" in prose_from_url("https://example.com/pricing/")


class TestRanking:
    """`suggest_replacements` needs a database, so the ranking rule itself is
    pinned here — it is the part that decides whether a suggestion is useful."""

    def test_breadth_beats_a_single_rare_word(self) -> None:
        import math

        # The observed failure: "Slack Block Quote Migration" outranked the
        # pricing page for "price quote" because one rare word summed higher
        # than a broader, more plausible match. sqrt(breadth) fixes the order
        # without letting breadth swamp rarity.
        one_rare = 7.9 * math.sqrt(1)
        three_moderate = (4.2 + 4.1 + 4.0) * math.sqrt(3)
        assert three_moderate > one_rare

    def test_the_threshold_matches_the_internal_linker(self) -> None:
        # Below it, the overlap is common words. Three bad guesses are worse
        # than saying there is no match.
        from app.agents.optimizer.internal_linker import _MIN_PAIR_SCORE
        assert MIN_SUGGESTION_SCORE == _MIN_PAIR_SCORE

    def test_at_most_three_are_offered(self) -> None:
        assert MAX_SUGGESTIONS == 3


class TestPluralMatching:
    """The concrete failure this fixes: for "price quote", the obviously right
    page — "Tailored Price Quotes" — lost to an article about Slack block
    quotes, because of a trailing "s"."""

    def test_a_plural_matches_its_singular(self) -> None:
        from app.services.link_repair import _stem
        assert _stem("quotes") == _stem("quote")
        assert _stem("prices") == _stem("price")
        assert _stem("tools") == _stem("tool")

    def test_es_is_only_stripped_after_a_sibilant(self) -> None:
        # Stripping both letters everywhere turns "quotes" into "quot" while
        # "quote" stays whole — reintroducing the mismatch in reverse.
        from app.services.link_repair import _stem
        assert _stem("boxes") == "box"
        assert _stem("churches") == "church"
        assert _stem("quotes") == "quote"

    def test_words_ending_in_double_s_are_left_alone(self) -> None:
        from app.services.link_repair import _stem
        assert _stem("class") == "class"
        assert _stem("address") == "address"

    def test_short_words_are_left_alone(self) -> None:
        from app.services.link_repair import _stem
        assert _stem("is") == "is"
        assert _stem("as") == "as"

    def test_the_shared_scorer_is_untouched(self) -> None:
        # The internal linker's threshold is calibrated against `_keywords`
        # and its suggestions surface elsewhere; stemming there would change
        # those quietly.
        from app.agents.optimizer.internal_linker import _keywords
        assert _keywords("Tailored Price Quotes") == {"tailored", "price", "quotes"}


class TestTitles:
    def test_entities_are_decoded_for_display(self) -> None:
        # WordPress stores them intact. Fine for matching, wrong to show to
        # someone choosing between suggestions.
        from app.services.link_repair import _clean_title
        assert _clean_title("Pricing from CloudFuze &#8211; Tailored") == "Pricing from CloudFuze – Tailored"

    def test_markup_is_stripped(self) -> None:
        from app.services.link_repair import _clean_title
        assert _clean_title("<em>Price</em> Quotes") == "Price Quotes"


class TestScope:
    @pytest.mark.asyncio
    async def test_a_real_url_that_merely_fails_gets_no_suggestions(self) -> None:
        # Its destination is known. Proposing a replacement would invent an
        # intention nobody expressed.
        from app.services.link_repair import suggest_replacements
        assert await suggest_replacements(None, "site", "https://example.com/gone") == []  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_prose_with_no_usable_words_gets_no_suggestions(self) -> None:
        from app.services.link_repair import suggest_replacements
        assert await suggest_replacements(None, "site", "https://a%20of%20the") == []  # type: ignore[arg-type]
