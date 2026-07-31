"""Research-oriented page guidance — the AI layer that reads the actual page
and produces what rules cannot.

The rule engine (insights.py) already tells the user WHAT is wrong: CTR below
par, queries in striking distance, stale content. Restating those in prose
adds nothing, which is exactly what a metrics-only prompt produces.

So this pass is given three things the rule engine never has:
  1. The page's real body text, fetched live from WordPress.
  2. The exact search queries it surfaces for, with impressions and position.
  3. The findings already on screen — each one to be ANSWERED with a concrete
     fix, never repeated back.

Its job is therefore different in kind: read the content against the demand,
and return artifacts — a title someone can paste, a meta description someone
can paste, gaps between what searchers ask and what the page says, and the
specific change that resolves each known problem.
"""
import logging
import re
from typing import Any

from app.ai.engine import MODEL, ai_engine

logger = logging.getLogger(__name__)

# The body is the expensive part of this prompt. 8k characters is roughly a
# 1,200-word article — enough to judge structure and coverage, while keeping
# a single generation bounded and predictable in cost.
_MAX_CONTENT_CHARS = 8000
_MAX_QUERIES = 25

_TAG_RE = re.compile(r"<[^>]+>")
_WS_RE = re.compile(r"\s+")


def strip_html(html: str) -> str:
    """Readable text from WordPress HTML. Script/style bodies are dropped
    entirely rather than flattened — otherwise minified JS lands in the
    prompt and gets mistaken for page copy."""
    if not html:
        return ""
    text = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", html, flags=re.I | re.S)
    text = _TAG_RE.sub(" ", text)
    text = (
        text.replace("&nbsp;", " ").replace("&amp;", "&")
        .replace("&lt;", "<").replace("&gt;", ">").replace("&#8217;", "'")
    )
    return _WS_RE.sub(" ", text).strip()


def _headings(html: str) -> list[str]:
    """The page's actual H2/H3 text — the structural skeleton, which is what
    a "you're missing a section on X" judgement needs to be grounded in."""
    if not html:
        return []
    found = re.findall(r"<h([23])[^>]*>(.*?)</h\1>", html, flags=re.I | re.S)
    return [strip_html(text) for _level, text in found if strip_html(text)][:25]


def build_guidance_prompt(
    *,
    title: str,
    url: str,
    content_html: str,
    meta_description: str | None,
    metrics: dict[str, Any],
    queries: list[dict[str, Any]],
    known_findings: list[str],
    site_context: dict[str, Any] | None = None,
) -> str:
    body = strip_html(content_html)
    truncated = len(body) > _MAX_CONTENT_CHARS
    body_excerpt = body[:_MAX_CONTENT_CHARS]
    headings = _headings(content_html)

    query_lines = "\n".join(
        f"  - \"{q['query']}\" — {q['impressions']:,} impressions, position {q['position']:.1f}, "
        f"{q['clicks']} clicks ({q['ctr']:.2f}% CTR)"
        for q in queries[:_MAX_QUERIES]
    ) or "  (no search query data available)"

    metric_lines = "\n".join(f"  {k}: {v}" for k, v in metrics.items() if v is not None) or "  (none)"

    # The single most important instruction in this prompt. Without it the
    # model re-derives the rule findings from the same numbers and returns
    # them as advice the user is already looking at.
    findings_block = ""
    if known_findings:
        findings_block = (
            "KNOWN PROBLEMS (already displayed to the user — they can see this list)\n"
            "For each one, give the SPECIFIC fix for THIS page: the exact questions to add,\n"
            "the exact sections to expand, the exact topics to link to.\n"
            "Never restate the problem — that adds nothing they don't already have on screen.\n"
            + "\n".join(f"  - {f}" for f in known_findings)
            + "\n\n"
        )

    site_block = ""
    if site_context and site_context.get("summary"):
        site_block = (
            f"SITE: {site_context.get('business_type', '')} in {site_context.get('industry', '')}, "
            f"selling {', '.join(site_context.get('primary_offerings', [])[:4])}, "
            f"for {site_context.get('target_audience', 'their audience')}. "
            f"Brand tone: {site_context.get('brand_tone', 'professional')}.\n\n"
        )

    return f"""You are an SEO content strategist reviewing ONE page in detail. You have the page's actual text, the exact queries it appears for in Google, and its measured performance. Produce concrete, ready-to-use improvements — not a summary of the problems.

{site_block}PAGE
  URL   : {url}
  Title : {title}
  Meta  : {meta_description or "(none set)"}

MEASURED PERFORMANCE
{metric_lines}

SEARCH QUERIES THIS PAGE ACTUALLY APPEARS FOR (ranked by impressions)
{query_lines}

PAGE HEADINGS
{chr(10).join(f"  - {h}" for h in headings) if headings else "  (no H2/H3 headings found)"}

PAGE CONTENT{" (truncated)" if truncated else ""}
\"\"\"
{body_excerpt}
\"\"\"

{findings_block}YOUR TASK — read the content AGAINST the queries and answer:
1. Does the title match the language people actually search? Propose a better one if not.
2. Does the meta description promise what searchers want? Propose a better one.
3. Which high-impression queries does the content NOT actually answer well? Read the
   body and headings before claiming a gap — only flag a topic genuinely thin or absent.
4. For EVERY known problem listed above, give its concrete fix for this specific page.
   Cover all of them — a problem with no fix leaves the user stuck.

Rules:
- Base every claim on the content and queries above. Never invent statistics or facts.
- The proposed title must be under 60 characters and read naturally, not keyword-stuffed.
- The proposed meta description must be 120-160 characters.
- Quote real queries as evidence. If a gap isn't supported by a listed query, omit it.
- If the current title or meta is already good, say so and repeat it unchanged rather
  than inventing a worse alternative.
- 2 to 4 content gaps. Fewer is fine; do not pad.
- Fixes must be executable as written. "Add FAQ schema" is useless; naming the actual
  questions to mark up — drawn from the real queries above — is not.

Respond with JSON only:
{{
  "diagnosis": "<2-3 sentences: what is actually holding this page back, grounded in the data>",
  "title": {{"proposed": "<title under 60 chars>", "reason": "<why, citing a real query>"}},
  "meta_description": {{"proposed": "<120-160 chars>", "reason": "<why>"}},
  "content_gaps": [
    {{"topic": "<what's missing>", "evidence": "<query + impressions + position>", "add": "<specific section or heading to add, and what it should cover>"}}
  ],
  "fixes": [
    {{"problem": "<which known problem, in 2-4 words>", "fix": "<the exact change to make on this page, specific enough to act on without further thought>"}}
  ]
}}"""


def _clean_gap(gap: Any) -> dict[str, str] | None:
    if not isinstance(gap, dict):
        return None
    topic = str(gap.get("topic") or "").strip()
    if not topic:
        return None
    return {
        "topic": topic,
        "evidence": str(gap.get("evidence") or "").strip(),
        "add": str(gap.get("add") or "").strip(),
    }


def _clean_fix(fix: Any) -> dict[str, str] | None:
    """A fix is only worth showing if it says what to DO — an entry with a
    problem name and no action is the restatement this whole pass exists to
    avoid."""
    if not isinstance(fix, dict):
        return None
    action = str(fix.get("fix") or "").strip()
    if not action:
        return None
    return {"problem": str(fix.get("problem") or "").strip(), "fix": action}


def normalize_guidance(raw: dict[str, Any] | None) -> dict[str, Any] | None:
    """Coerce the model's JSON into the exact shape the UI renders.

    Returns None when nothing usable came back, so callers can distinguish a
    failed generation from a genuinely empty result and leave existing
    guidance untouched rather than wiping it.
    """
    if not raw:
        return None

    def _section(key: str, optimal: tuple[int, int]) -> dict[str, Any] | None:
        val = raw.get(key)
        if not isinstance(val, dict):
            return None
        proposed = str(val.get("proposed") or "").strip()
        if not proposed:
            return None
        # Models routinely drift outside the requested character range. The
        # suggestion is still useful, so it's kept — but the length travels
        # with it so the UI can flag it rather than presenting an
        # out-of-spec string as if it were ready to paste.
        low, high = optimal
        return {
            "proposed": proposed,
            "reason": str(val.get("reason") or "").strip(),
            "length": len(proposed),
            "in_range": low <= len(proposed) <= high,
            "optimal": f"{low}–{high}",
        }

    gaps = [g for g in (_clean_gap(g) for g in (raw.get("content_gaps") or [])) if g]
    fixes = [f for f in (_clean_fix(f) for f in (raw.get("fixes") or [])) if f]
    guidance = {
        "diagnosis": str(raw.get("diagnosis") or "").strip(),
        # Google truncates titles around 60 chars and descriptions around 160.
        "title": _section("title", (30, 60)),
        "meta_description": _section("meta_description", (120, 160)),
        "content_gaps": gaps[:4],
        # Generous by design: the prompt asks for a fix per known problem, so
        # a cap below the number of findings silently drops one — which is
        # precisely the "my FAQ schema issue is missing" complaint this pass
        # exists to answer. Findings top out around a dozen in practice.
        "fixes": fixes[:16],
    }
    # Nothing actionable came back — treat as a failure, not an empty success.
    if (
        not guidance["diagnosis"] and not guidance["title"]
        and not guidance["meta_description"] and not gaps and not fixes
    ):
        return None
    return guidance


def guidance_as_text(guidance: dict[str, Any]) -> str:
    """Plain-text rendering for `ContentPost.ai_recommendation`, which other
    surfaces (the SEO opportunities list) show as prose. Keeps that field
    meaningful instead of leaking raw JSON into an unrelated view."""
    lines: list[str] = []
    if guidance.get("title"):
        lines.append(f"Retitle: \"{guidance['title']['proposed']}\" — {guidance['title']['reason']}")
    if guidance.get("meta_description"):
        lines.append(f"Meta description: {guidance['meta_description']['reason']}")
    for gap in guidance.get("content_gaps", [])[:2]:
        lines.append(f"Add: {gap['topic']} — {gap['add']}")
    for fix in guidance.get("fixes", [])[:2]:
        lines.append(f"{fix['problem']}: {fix['fix']}" if fix["problem"] else fix["fix"])
    return "\n".join(lines[:3])


async def generate_page_guidance(**kwargs: Any) -> dict[str, Any] | None:
    """One page, one generation. Uses the flagship model deliberately: this
    reads a full article and reasons about coverage gaps, and — unlike the
    old bulk pass over thousands of posts — runs only when a user asks for
    it, so the quality is worth far more than the token saving."""
    prompt = build_guidance_prompt(**kwargs)
    try:
        raw = await ai_engine.generate_json(prompt, max_tokens=1800, model=MODEL)
    except Exception as exc:
        logger.error("Page guidance generation failed: %s", exc)
        return None
    if not raw:
        logger.error("Page guidance returned an empty/unparseable response")
        return None
    return normalize_guidance(raw)


__all__ = [
    "build_guidance_prompt", "generate_page_guidance", "guidance_as_text",
    "normalize_guidance", "strip_html",
]
