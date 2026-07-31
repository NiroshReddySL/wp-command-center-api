"""Research-oriented page guidance — the on-demand AI pass that reads the
real page against real search demand.

Its whole value is being DIFFERENT from the rule engine. A metrics-only
prompt just re-derives the same findings and hands them back as advice the
user is already looking at, so the prompt must carry the page's actual text,
the real queries, and an explicit list of what's already on screen.
"""
from app.agents.optimizer.guidance import (
    build_guidance_prompt,
    guidance_as_text,
    normalize_guidance,
    strip_html,
)

QUERIES = [
    {"query": "delete photos from onedrive", "impressions": 70, "position": 4.3, "clicks": 2, "ctr": 2.9},
    {"query": "how to delete all photos in onedrive", "impressions": 58, "position": 6.0, "clicks": 1, "ctr": 1.7},
]


def _prompt(**over):
    base = dict(
        title="Manage and Delete Photos in OneDrive",
        url="https://example.com/onedrive/",
        content_html="<h2>Why Manage OneDrive?</h2><p>Some body copy about storage.</p>",
        meta_description="An existing meta description.",
        metrics={"Search clicks (28d)": 99, "Avg position": 6.7},
        queries=QUERIES,
        known_findings=["Ranking well, rarely clicked — 0.42% CTR at position 6.7"],
    )
    base.update(over)
    return build_guidance_prompt(**base)


class TestStripHtml:
    def test_extracts_readable_text(self) -> None:
        assert strip_html("<p>Hello <b>world</b></p>") == "Hello world"

    def test_drops_script_and_style_bodies_entirely(self) -> None:
        # Flattening these would feed minified JS into the prompt, where it
        # reads as page copy and corrupts the content-gap judgement.
        html = "<p>Real copy</p><script>var x=1;alert('spam')</script><style>.a{color:red}</style>"
        out = strip_html(html)
        assert "Real copy" in out
        assert "alert" not in out and "color:red" not in out

    def test_decodes_common_entities(self) -> None:
        assert strip_html("<p>Tom&#8217;s &amp; Jerry&nbsp;files</p>") == "Tom's & Jerry files"

    def test_empty_input_is_safe(self) -> None:
        assert strip_html("") == ""


class TestBuildGuidancePrompt:
    def test_includes_the_real_page_text(self) -> None:
        assert "Some body copy about storage" in _prompt()

    def test_includes_the_pages_actual_headings(self) -> None:
        # Grounding "you're missing a section on X" in the real structure is
        # what stops the model inventing gaps that already have coverage.
        assert "Why Manage OneDrive?" in _prompt()

    def test_includes_queries_with_their_evidence(self) -> None:
        p = _prompt()
        assert "delete photos from onedrive" in p
        assert "70 impressions" in p
        assert "position 4.3" in p

    def test_demands_a_fix_for_each_known_problem_not_a_restatement(self) -> None:
        # The single most important instruction. Without it the output either
        # duplicates the "What to fix" list, or (if merely told to avoid it)
        # skips those problems entirely and leaves the user stuck.
        p = _prompt()
        assert "Never restate the problem" in p
        assert "For EVERY known problem" in p
        assert "Ranking well, rarely clicked" in p

    def test_findings_block_is_omitted_when_there_are_none(self) -> None:
        assert "KNOWN PROBLEMS" not in _prompt(known_findings=[])

    def test_demands_executable_fixes_not_restated_labels(self) -> None:
        # "Add FAQ schema" is the problem restated; the actual questions to
        # mark up are the fix.
        assert "executable as written" in _prompt()

    def test_states_the_current_title_and_meta_for_comparison(self) -> None:
        p = _prompt()
        assert "Manage and Delete Photos in OneDrive" in p
        assert "An existing meta description." in p

    def test_missing_meta_is_labelled_not_blank(self) -> None:
        assert "(none set)" in _prompt(meta_description=None)

    def test_long_content_is_truncated_and_flagged(self) -> None:
        p = _prompt(content_html="<p>" + ("word " * 5000) + "</p>")
        assert "(truncated)" in p

    def test_short_content_is_not_flagged_as_truncated(self) -> None:
        assert "(truncated)" not in _prompt()

    def test_survives_a_page_with_no_search_data(self) -> None:
        assert "no search query data available" in _prompt(queries=[])


class TestNormalizeGuidance:
    def test_nothing_usable_returns_none(self) -> None:
        # None means "generation failed", so callers keep existing guidance
        # rather than wiping it with an empty result.
        assert normalize_guidance(None) is None
        assert normalize_guidance({}) is None
        assert normalize_guidance({"diagnosis": "", "content_gaps": []}) is None

    def test_flags_a_meta_description_outside_the_optimal_range(self) -> None:
        # Observed in a real generation: the model returned 116 chars against
        # a requested 120-160. Still useful, but must not be presented as
        # ready to paste without saying so.
        out = normalize_guidance({
            "diagnosis": "d",
            "meta_description": {"proposed": "x" * 116, "reason": "r"},
        })
        assert out["meta_description"]["length"] == 116
        assert out["meta_description"]["in_range"] is False
        assert out["meta_description"]["optimal"] == "120–160"

    def test_accepts_an_in_range_meta_description(self) -> None:
        out = normalize_guidance({
            "diagnosis": "d", "meta_description": {"proposed": "x" * 140, "reason": "r"},
        })
        assert out["meta_description"]["in_range"] is True

    def test_title_range_is_checked_independently(self) -> None:
        out = normalize_guidance({
            "diagnosis": "d", "title": {"proposed": "x" * 47, "reason": "r"},
        })
        assert out["title"]["in_range"] is True
        assert out["title"]["optimal"] == "30–60"

    def test_a_section_with_no_proposal_is_dropped(self) -> None:
        out = normalize_guidance({"diagnosis": "d", "title": {"reason": "no proposal"}})
        assert out["title"] is None

    def test_malformed_gaps_are_discarded_not_rendered_blank(self) -> None:
        out = normalize_guidance({
            "diagnosis": "d",
            "content_gaps": ["a string", {"no_topic": 1}, {"topic": "Real gap", "add": "Do this"}],
        })
        assert [g["topic"] for g in out["content_gaps"]] == ["Real gap"]

    def test_keeps_fixes_that_say_what_to_do(self) -> None:
        out = normalize_guidance({
            "diagnosis": "d",
            "fixes": [{"problem": "Missing FAQ schema", "fix": "Mark up the 3 questions already in the body"}],
        })
        assert out["fixes"][0]["problem"] == "Missing FAQ schema"

    def test_discards_a_fix_with_no_action(self) -> None:
        # A problem name with no action is exactly the restatement this pass
        # exists to avoid, so it must never reach the UI.
        out = normalize_guidance({
            "diagnosis": "d",
            "fixes": [{"problem": "Thin content", "fix": "  "}, "junk", {"fix": "Do the thing"}],
        })
        assert [f["fix"] for f in out["fixes"]] == ["Do the thing"]

    def test_fixes_are_not_capped_below_a_realistic_finding_count(self) -> None:
        # Regression: a cap of 8 silently dropped the 9th fix on a page with
        # 9 findings — the "my FAQ schema issue is missing" complaint.
        out = normalize_guidance({
            "diagnosis": "d",
            "fixes": [{"problem": f"p{i}", "fix": f"do {i}"} for i in range(12)],
        })
        assert len(out["fixes"]) == 12

    def test_fixes_alone_are_enough_to_count_as_a_result(self) -> None:
        out = normalize_guidance({"fixes": [{"problem": "p", "fix": "do it"}]})
        assert out is not None

    def test_gaps_are_capped(self) -> None:
        out = normalize_guidance({
            "diagnosis": "d",
            "content_gaps": [{"topic": f"gap {i}"} for i in range(9)],
        })
        assert len(out["content_gaps"]) == 4


class TestGuidanceAsText:
    """`ai_recommendation` stays plain prose because other surfaces render it
    directly — raw JSON leaking into the SEO opportunities list would be a
    visible bug."""

    def test_renders_prose_not_json(self) -> None:
        text = guidance_as_text({
            "title": {"proposed": "Better Title", "reason": "matches the top query"},
            "meta_description": {"proposed": "x", "reason": "clearer promise"},
            "content_gaps": [{"topic": "Bulk delete", "evidence": "e", "add": "Add a section"}],
        })
        assert "{" not in text and "}" not in text
        assert "Better Title" in text

    def test_caps_at_three_lines(self) -> None:
        text = guidance_as_text({
            "title": {"proposed": "T", "reason": "r"},
            "meta_description": {"proposed": "m", "reason": "r"},
            "content_gaps": [{"topic": f"g{i}", "evidence": "e", "add": "a"} for i in range(4)],
        })
        assert len(text.split("\n")) <= 3

    def test_empty_guidance_yields_empty_string(self) -> None:
        assert guidance_as_text({"content_gaps": []}) == ""
