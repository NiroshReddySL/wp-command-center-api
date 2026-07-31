"""Deterministic page insights — what's wrong with a page and what to do
about it, computed by rule from data already measured.

Why rules rather than a model: these findings are the page's permanent
baseline, shown on every visit. An LLM call per page-view would be slow,
costly and — worse — non-deterministic, so the same unchanged page could
report different problems on refresh. Every rule here is arithmetic over
numbers we already hold, so it is instant, free, repeatable and cannot
hallucinate a problem that isn't there.

The AI layer sits ON TOP of this, on explicit request: it turns these
findings into prose and suggests specific rewrites. It never replaces them.

Every source is optional and independent — a site with no Google Analytics,
no Search Console or no PageSpeed test still gets every finding the data it
does have can support.
"""
from typing import Any

# Ordering: severity first, then estimated impact. "Impact" is a rough
# clicks-or-visits-at-stake figure used ONLY for ranking findings against
# each other — it is never shown as a prediction.
_SEVERITY_RANK = {"critical": 0, "warning": 1, "info": 2}

# A ranking page that nobody clicks is usually worth more than any content
# tweak, so search findings carry real click estimates while content findings
# are weighted by the traffic they'd affect.
_STRIKING_DISTANCE_TARGET_CTR = 0.03  # what those queries might earn nearer the top

# Content categories, mapped to the action each one implies. Keyed by the
# score_breakdown keys the ContentScorer already produces.
_CONTENT_ACTIONS: dict[str, tuple[str, str]] = {
    "word_count": ("Thin content", "Expand the article with genuinely useful depth"),
    "freshness": ("Stale content", "Refresh the facts and re-publish"),
    "schema_markup": ("Schema markup", "Add the missing structured data"),
    "meta_description": ("Meta description", "Write a meta description in the 120–160 character range"),
    "title": ("Title length", "Bring the title into the 30–60 character range"),
    "headings": ("Heading structure", "Fix the heading hierarchy"),
    "images": ("Images", "Add relevant images, including a featured image"),
    "links": ("Internal links", "Add internal links to related posts"),
}


def _insight(
    id: str, source: str, severity: str, title: str,
    detail: str, action: str, evidence: str, impact: float,
) -> dict[str, Any]:
    return {
        "id": id, "source": source, "severity": severity, "title": title,
        "detail": detail, "action": action, "evidence": evidence,
        "impact": round(impact),
    }


def _search_insights(search: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not search:
        return []
    out: list[dict[str, Any]] = []

    opp = search.get("ctr_opportunity")
    if opp:
        missed = max(0, opp["potential_clicks"] - (search.get("clicks") or 0))
        out.append(_insight(
            id="ctr_gap", source="search",
            # A page already on page one leaking hundreds of clicks is the
            # most valuable fix available; a smaller leak is still worth
            # doing but shouldn't outrank a broken page.
            severity="critical" if missed >= 100 else "warning",
            title="Ranking well, rarely clicked",
            detail=(
                f"This page ranks at position {opp['position']:.1f} but earns only "
                f"{opp['ctr']:.2f}% CTR, against roughly {opp['typical_ctr']:.1f}% typical "
                f"for that position. It already ranks — the title and meta description "
                f"are what's losing the clicks."
            ),
            action="Rewrite the title and meta description to match search intent",
            evidence=f"{opp['ctr']:.2f}% CTR at position {opp['position']:.1f}",
            impact=missed,
        ))

    striking = search.get("striking_distance") or []
    if striking:
        impressions = sum(q["impressions"] for q in striking)
        top = ", ".join(f'"{q["query"]}"' for q in striking[:3])
        out.append(_insight(
            id="striking_distance", source="search", severity="warning",
            title=f"{len(striking)} queries just outside the top results",
            detail=(
                f"Ranking positions 4–15 for {len(striking)} queries with {impressions:,} "
                f"combined impressions — including {top}. These are the closest available "
                f"traffic wins."
            ),
            action="Strengthen the sections covering these queries",
            evidence=f"{impressions:,} impressions in positions 4–15",
            impact=impressions * _STRIKING_DISTANCE_TARGET_CTR,
        ))

    slipped = search.get("position_change")
    if slipped is not None and slipped > 1.5:
        out.append(_insight(
            id="position_slipped", source="search", severity="warning",
            title="Slipping in search results",
            detail=(
                f"Average position fell by {slipped:.1f} versus the previous period — "
                f"competitors are gaining ground on these queries."
            ),
            action="Review what newly-ranking competitors cover that this doesn't",
            evidence=f"Position dropped {slipped:.1f}",
            impact=search.get("clicks") or 0,
        ))

    return out


def _speed_insights(speed: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not speed or speed.get("score") is None:
        return []
    score = speed["score"]
    failing = speed.get("failing_vitals") or []
    if not failing and score >= 90:
        return []

    return [_insight(
        id="pagespeed", source="speed",
        severity="critical" if score < 50 else "warning",
        title=f"PageSpeed {score}/100",
        detail=(
            f"Core Web Vitals failing: {', '.join(failing)}."
            if failing else
            f"Lighthouse scores this page {score}/100 on {speed.get('strategy', 'desktop')}."
        ),
        action="Fix the failing Core Web Vitals" if failing else "Investigate what's slowing the page",
        evidence=f"{score}/100 ({speed.get('strategy', 'desktop')})",
        # Slow pages cost conversions across everyone who lands, so scale by
        # the audience actually affected.
        impact=(speed.get("visitors") or 0) * (0.5 if score < 50 else 0.2),
    )]


def _traffic_insights(traffic: dict[str, Any] | None) -> list[dict[str, Any]]:
    if not traffic:
        return []
    out: list[dict[str, Any]] = []
    visitors = traffic.get("visitors") or 0

    bounce = traffic.get("bounce_rate")
    if bounce is not None and bounce > 70 and visitors > 0:
        out.append(_insight(
            id="high_bounce", source="traffic", severity="warning",
            title=f"{bounce:.0f}% of readers leave immediately",
            detail=(
                f"A {bounce:.0f}% bounce rate means the page rarely delivers what its "
                f"visitors expected, or gives them nowhere to go next."
            ),
            action="Match the opening to the search intent and add a clear next step",
            evidence=f"{bounce:.0f}% bounce rate",
            impact=visitors * 0.5,
        ))

    engagement = traffic.get("avg_engagement_time")
    if engagement is not None and 0 < engagement < 30 and visitors > 0:
        out.append(_insight(
            id="low_engagement", source="traffic", severity="warning",
            title=f"Readers stay only {engagement:.0f} seconds",
            detail=(
                f"An average of {engagement:.0f}s is far below the time needed to read this "
                f"page — most visitors are leaving before they reach the substance."
            ),
            action="Front-load the answer instead of building up to it",
            evidence=f"{engagement:.0f}s average engagement",
            impact=visitors * 0.4,
        ))

    leads = traffic.get("leads")
    if leads == 0 and visitors >= 10:
        out.append(_insight(
            id="no_conversions", source="traffic", severity="info",
            title="Traffic but no conversions",
            detail=(
                f"{visitors:,} people read this and none reached your confirmation page. "
                f"The page attracts interest it isn't converting."
            ),
            action="Add a relevant call to action for what this reader wants next",
            evidence=f"0 conversions from {visitors:,} readers",
            impact=visitors * 0.3,
        ))

    return out


def _content_insights(
    breakdown: dict[str, Any] | None,
    visitors: int,
    issues: list[str] | None = None,
) -> list[dict[str, Any]]:
    """One finding per content problem, from two sources that each miss
    things the other catches.

    `breakdown` carries a per-category `status`, but not every category sets
    one — schema_markup records a missing FAQPage in `faq_recommendation`
    with no status at all, so a status-only scan silently dropped it. The
    scorer's own `issues` list is the authoritative set of what's wrong, so
    it's used as a backstop: anything it flagged that no category emitted
    still gets surfaced rather than disappearing between the two.
    """
    out: list[dict[str, Any]] = []
    covered: set[str] = set()
    # A floor so a brand-new page with no traffic still surfaces its content
    # problems; otherwise weighted by the readers affected.
    impact = max(visitors * 0.1, 1)

    for key, data in (breakdown or {}).items():
        if not isinstance(data, dict):
            continue
        status = data.get("status")
        if status not in ("critical", "warning"):
            continue
        label, action = _CONTENT_ACTIONS.get(key, (key.replace("_", " ").title(), "Review this"))
        detail = data.get("detail") or ""
        covered.add(key)
        out.append(_insight(
            id=f"content_{key}", source="content",
            # A structural flaw is a lower priority than a measured loss:
            # content problems are inferred, search/speed problems are
            # observed. Critical content still outranks warnings.
            severity=status,
            title=label,
            detail=detail,
            action=action,
            evidence=detail,
            impact=impact,
        ))

    # Structured check for the category that carries its verdict outside
    # `status` — a missing FAQPage schema on a page whose content is visibly
    # Q&A shaped is a real, fixable rich-result opportunity.
    schema = (breakdown or {}).get("schema_markup")
    if (
        isinstance(schema, dict)
        and schema.get("faq_recommendation") == "missing"
        and "schema_markup" not in covered
    ):
        covered.add("schema_markup")
        out.append(_insight(
            id="content_faq_schema", source="content", severity="warning",
            title="Missing FAQ schema",
            detail=(
                "This page has FAQ-style content but no FAQPage structured data, so Google "
                "can't show it as a rich result with expandable answers."
            ),
            action="Add FAQPage schema covering the questions this page answers",
            evidence=schema.get("detail") or "FAQ content found — schema missing",
            impact=impact,
        ))

    # Backstop: anything the scorer flagged that nothing above emitted.
    for issue in issues or []:
        if any(kw in issue.lower() for kw in _ISSUE_KEYWORDS_COVERED(covered)):
            continue
        out.append(_insight(
            id=f"issue_{abs(hash(issue)) % 10**8}", source="content", severity="warning",
            title=issue.split("—")[0].split("(")[0].strip()[:60] or "Content issue",
            detail=issue,
            action="Review and resolve this issue",
            evidence=issue,
            impact=impact,
        ))
    return out


# Which issue-text keywords each breakdown category already accounts for.
# Used only to avoid double-reporting the same problem from the backstop
# above — never to decide whether a problem exists.
_CATEGORY_ISSUE_KEYWORDS: dict[str, tuple[str, ...]] = {
    "word_count": ("short content", "thin content", "words"),
    "images": ("image",),
    "links": ("link",),
    "freshness": ("stale", "updated"),
    "title": ("title",),
    "headings": ("heading", "h1", "h2"),
    "meta_description": ("meta description",),
    "schema_markup": ("schema", "faq"),
}


def _ISSUE_KEYWORDS_COVERED(covered: set[str]) -> tuple[str, ...]:
    return tuple(kw for key in covered for kw in _CATEGORY_ISSUE_KEYWORDS.get(key, ()))


def build_insights(
    breakdown: dict[str, Any] | None = None,
    traffic: dict[str, Any] | None = None,
    search: dict[str, Any] | None = None,
    speed: dict[str, Any] | None = None,
    issues: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Every finding across all four sources, most important first.

    Ranked by severity, then by estimated impact — so a page leaking 200
    clicks a month leads over a missing meta description, without the
    content finding being lost.
    """
    visitors = (traffic or {}).get("visitors") or 0
    insights = [
        *_search_insights(search),
        *_speed_insights(speed),
        *_traffic_insights(traffic),
        *_content_insights(breakdown, visitors, issues),
    ]
    return sorted(
        insights,
        key=lambda i: (_SEVERITY_RANK.get(i["severity"], 9), -i["impact"]),
    )
