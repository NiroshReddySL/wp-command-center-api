"""Search, conversion, metadata and structured-data sections.

The CTR arithmetic here is the one place a report can most easily mislead, so
two rules are enforced throughout. Period CTR is total clicks over total
impressions, never a mean of daily rates — a mean weights a quiet Sunday the
same as a busy Tuesday. And an uplift scenario is stated as arithmetic on
existing impressions, never as a forecast, because nothing in the data
supports a claim about what will happen.
"""
from collections import Counter
from typing import Any

from sqlalchemy import select

from app.reports.analysed import ANALYSED
from app.reports.context import ReportContext
from app.reports.models import Finding, Metric, Section, Table

# Where a page can realistically move with metadata and content work alone.
_STRIKING_LOW, _STRIKING_HIGH = 4.0, 15.0
_CTR_TARGET_UPLIFT = 1.3  # the scenario multiplier, stated as arithmetic


def _pct(part: int, whole: int) -> str:
    return f"{(part / whole * 100):.1f}%" if whole else "—"


# ── Organic search performance ───────────────────────────────────────────────

async def build_search(ctx: ReportContext) -> Section:
    section = Section(
        key="search", number="01", title="Organic Search Performance",
        headline="No search data is available for this period.",
    )
    totals = ctx.search_totals
    if not totals:
        section.unavailable = ctx.search_error or "No Search Console data returned."
        return section

    clicks, impressions = int(totals["clicks"]), int(totals["impressions"])
    ctr, position, days = totals["ctr"], totals["position"], int(totals["days"])

    section.headline = (
        f"{impressions:,} impressions produced {clicks:,} clicks over {days} days, "
        f"a {ctr:.2f}% click-through rate."
    )
    basis = f"Search Console total over {days} days with data in {ctx.period.label}"
    section.metrics = [
        Metric("Impressions", impressions, basis),
        Metric("Clicks", clicks, basis),
        Metric("Click-through rate", round(ctr, 2),
               "total clicks divided by total impressions", unit="%",
               sub="not a mean of daily rates"),
        Metric("Average position", round(position, 1),
               "impression-weighted mean position",
               sub="weighted so high-volume days count for more"),
    ]
    section.notes.append(
        "Click-through rate is computed from period totals. A mean of daily rates would "
        "weight a quiet day the same as a busy one and read differently for the same data."
    )
    shortfall = ctx.period.shortfall_note(days)
    if shortfall:
        # Search Console finalises 2-3 days late, so a period ending today is
        # always short. Reporting the total without saying so understates it
        # silently, which reads as a decline that did not happen.
        section.notes.append(
            f"{shortfall} Search Console finalises data two to three days in arrears, so a "
            "period ending recently will always be missing its most recent days."
        )

    queries = ctx.search_queries or []
    if queries:
        top = sorted(queries, key=lambda q: int(q.get("clicks") or 0), reverse=True)[:15]
        section.tables.append(Table(
            "Highest-earning queries",
            ("Query", "Clicks", "Impressions", "CTR", "Position"),
            tuple(
                (str(q.get("query", "")), f"{int(q.get('clicks') or 0):,}",
                 f"{int(q.get('impressions') or 0):,}",
                 f"{float(q.get('ctr') or 0) * 100:.2f}%" if float(q.get("ctr") or 0) <= 1
                 else f"{float(q.get('ctr') or 0):.2f}%",
                 f"{float(q.get('position') or 0):.1f}")
                for q in top
            ),
            note=f"Top {len(top)} of {len(queries)} queries returned for the period.",
        ))

        # Positions 4–15: ranking already, not yet on the first screen. This is
        # the cohort where content and metadata work has somewhere to move to.
        striking = [
            q for q in queries
            if _STRIKING_LOW <= float(q.get("position") or 0) <= _STRIKING_HIGH
            and int(q.get("impressions") or 0) > 0
        ]
        if striking:
            striking_impressions = sum(int(q.get("impressions") or 0) for q in striking)
            section.metrics.append(Metric(
                "Queries in striking distance", len(striking),
                f"queries ranking between positions {_STRIKING_LOW:.0f} and {_STRIKING_HIGH:.0f}",
                sub=f"{striking_impressions:,} impressions",
            ))
            section.findings.append(Finding(
                id="SEARCH-02",
                title="A body of queries ranks just below the results people click",
                severity="opportunity",
                evidence=(
                    f"{len(striking)} queries rank between positions "
                    f"{_STRIKING_LOW:.0f} and {_STRIKING_HIGH:.0f}, carrying "
                    f"{striking_impressions:,} impressions — "
                    f"{_pct(striking_impressions, impressions)} of all impressions."
                ),
                implication=(
                    "These already rank. Moving them takes strengthening what exists rather "
                    "than earning new visibility, which is the cheaper of the two."
                ),
                actions=(
                    "Work the highest-impression queries in this band first.",
                    "Match the page to what the query actually asks before adding length.",
                    "Re-measure position by cohort rather than judging the site average.",
                ),
                measures=("Striking-distance queries reaching the top 3",
                          "Clicks from this cohort, tracked separately from the site total"),
                effort="Medium",
            ))

    # Stated as arithmetic on existing impressions — deliberately not a forecast.
    if impressions and ctr:
        target = ctr * _CTR_TARGET_UPLIFT
        extra = int(impressions * (target - ctr) / 100)
        if extra > 0:
            section.findings.append(Finding(
                id="SEARCH-01", title="Visibility is converting to clicks below its potential",
                severity="high" if ctr < 1.0 else "medium",
                evidence=(
                    f"{impressions:,} impressions returned {clicks:,} clicks, a {ctr:.2f}% "
                    f"click-through rate at an average position of {position:.1f}. At "
                    f"{target:.2f}% the same impressions would return roughly "
                    f"{extra:,} more clicks."
                ),
                implication=(
                    "Impressions are already being earned. Titles and descriptions decide "
                    "how many of them become visits, and they change far faster than rankings."
                ),
                actions=(
                    "Rewrite titles and meta descriptions on the highest-impression pages first.",
                    "Write to what the query asks rather than to the keyword.",
                    "Change one cohort at a time so the effect is attributable.",
                ),
                measures=(f"Click-through rate above {target:.2f}%",
                          "Clicks up without a fall in average position"),
                effort="Medium",
            ))
            section.notes.append(
                f"The {target:.2f}% figure is arithmetic on the impressions already recorded — "
                "what the same visibility would yield at a higher click-through rate. It is "
                "not a forecast, and nothing here predicts that it will happen."
            )

    return section


# ── Conversion & engagement ──────────────────────────────────────────────────

async def build_conversion(ctx: ReportContext) -> Section:
    section = Section(
        key="conversion", number="03", title="Landing Pages & Engagement",
        headline="No analytics data is available for this period.",
    )
    pages = ctx.ga_top_pages
    if not pages:
        section.unavailable = ctx.ga_error or "No Analytics data returned."
        return section

    def _int(row: dict[str, Any], *keys: str) -> int:
        for key in keys:
            if row.get(key) is not None:
                try:
                    return int(float(row[key]))
                except (TypeError, ValueError):
                    continue
        return 0

    total_views = sum(_int(p, "views", "pageviews", "screenPageViews") for p in pages)
    section.headline = (
        f"The {len(pages)} busiest pages drew {total_views:,} views over "
        f"{ctx.period.days} days."
    )
    section.metrics = [
        Metric("Pages measured", len(pages), "landing pages returned by Analytics"),
        Metric("Views across them", total_views,
               f"sum of views for these pages over {ctx.period.label}"),
    ]
    section.tables.append(Table(
        "Busiest landing pages",
        ("Page", "Views"),
        tuple(
            (str(p.get("path") or p.get("url") or p.get("page") or "—"),
             f"{_int(p, 'views', 'pageviews', 'screenPageViews'):,}")
            for p in pages
        ),
        note=f"Ranked by Analytics views over {ctx.period.label}.",
    ))
    section.notes.append(
        "These are the busiest pages, not the most valuable ones. Ranking by views says "
        "nothing about whether the visit led anywhere."
    )
    return section


# ── Metadata, headings & structured data ─────────────────────────────────────

async def build_metadata(ctx: ReportContext) -> Section:
    from app.database.models import ContentPost

    section = Section(
        key="metadata", number="06", title="Metadata, Headings & Structured Data",
        headline="No pages have been analysed.",
    )
    posts = (await ctx.db.execute(
        select(ContentPost).where(ContentPost.site_id == ctx.site.id, ANALYSED)
    )).scalars().all()
    if not posts:
        section.unavailable = "No pages have been analysed yet."
        return section

    total = len(posts)
    missing_meta = no_h1 = multi_h1 = long_title = short_title = 0
    no_schema = no_faq = thin_images = 0

    for post in posts:
        b = post.score_breakdown or {}
        meta = b.get("meta_description") or {}
        title = b.get("title") or {}
        headings = b.get("headings") or {}
        schema = b.get("schema_markup") or {}
        images = b.get("images") or {}

        if meta.get("status") in ("critical", "warning") or not meta.get("length"):
            missing_meta += 1
        length = title.get("length") or 0
        if length > 60:
            long_title += 1
        elif 0 < length < 30:
            short_title += 1
        h1 = headings.get("h1_count")
        if h1 == 0:
            no_h1 += 1
        elif isinstance(h1, int) and h1 > 1:
            multi_h1 += 1
        if not (schema.get("sources") or schema.get("type")):
            no_schema += 1
        if schema.get("faq_recommendation") == "missing":
            no_faq += 1
        if images.get("status") in ("critical", "warning"):
            thin_images += 1

    section.headline = (
        f"Across {total:,} analysed pages, {missing_meta:,} lack a usable meta description "
        f"and {no_h1:,} have no H1."
    )
    section.metrics = [
        Metric("Missing meta description", missing_meta,
               "pages with no description or one outside 120–160 characters",
               sub=_pct(missing_meta, total)),
        Metric("Titles over 60 chars", long_title,
               "pages whose title will be truncated in results", sub=_pct(long_title, total)),
        Metric("Missing H1", no_h1, "pages with no H1 heading", sub=_pct(no_h1, total)),
        Metric("No structured data", no_schema,
               "pages with no schema markup detected", sub=_pct(no_schema, total)),
    ]
    section.notes.append(
        f"Counted across the {total:,} pages that have been analysed. Pages not yet analysed "
        "are excluded rather than assumed clean."
    )

    rows = [
        ("Missing or out-of-range meta description", missing_meta),
        ("Title longer than 60 characters", long_title),
        ("Title shorter than 30 characters", short_title),
        ("No H1 heading", no_h1),
        ("More than one H1", multi_h1),
        ("No structured data detected", no_schema),
        ("FAQ content without FAQPage schema", no_faq),
        ("Image coverage flagged", thin_images),
    ]
    section.tables.append(Table(
        "Defects by type",
        ("Defect", "Pages affected", "Share of analysed pages"),
        tuple((label, f"{count:,}", _pct(count, total)) for label, count in rows if count),
    ))

    if missing_meta or long_title:
        section.findings.append(Finding(
            id="META-01", title="Search snippets are left to chance on many pages",
            severity="high" if (missing_meta + long_title) / total > 0.2 else "medium",
            evidence=(
                f"{missing_meta:,} pages have no usable meta description and {long_title:,} "
                f"have titles that will truncate — {_pct(missing_meta + long_title, total)} "
                f"of {total:,} analysed pages between them."
            ),
            implication=(
                "The title and description are the whole of what a searcher sees. Where they "
                "are absent Google writes its own, which is rarely the pitch you would choose."
            ),
            actions=(
                "Fix the highest-impression pages first rather than working alphabetically.",
                "Give each page type a title formula, then vary it per page.",
                "Treat truncation as a defect, not a style preference.",
            ),
            measures=("Every indexable page carries a description in range",
                      "No titles truncating in results"),
            effort="Large",
        ))

    if no_h1:
        section.findings.append(Finding(
            id="META-02", title="Pages are published without a primary heading",
            severity="medium",
            evidence=f"{no_h1:,} of {total:,} analysed pages ({_pct(no_h1, total)}) have no H1.",
            implication=(
                "An H1 states what the page is about, to readers, to screen readers and to "
                "search engines. Its absence usually points at a template rather than an author."
            ),
            actions=("Group the affected pages by template and fix the template.",
                     "Make the H1 describe the page, not repeat the site name."),
            measures=("No analysed page without an H1",),
            effort="Medium",
        ))

    if no_schema:
        section.findings.append(Finding(
            id="SCHEMA-01", title="Structured data is missing across much of the library",
            severity="medium",
            evidence=(
                f"{no_schema:,} of {total:,} analysed pages ({_pct(no_schema, total)}) carry no "
                f"detectable schema markup"
                + (f", and {no_faq:,} have FAQ-shaped content with no FAQPage markup" if no_faq else "")
                + "."
            ),
            implication=(
                "Structured data is how a page states what it is rather than leaving it to be "
                "inferred. It is also what rich results and AI answer extraction read first."
            ),
            actions=("Emit schema from templates rather than page by page.",
                     "Mark up existing FAQ content before writing new content.",
                     "Validate generated markup against the visible page."),
            measures=("Structured data on every indexable template",
                      "FAQ content marked up wherever it exists"),
            effort="Large",
        ))

    return section


# ── Markets & devices ────────────────────────────────────────────────────────

async def build_markets(ctx: ReportContext) -> Section:
    section = Section(
        key="markets", number="04", title="Markets & Devices",
        headline="No market or device breakdown is available.",
    )
    from app.database.models import TrafficSnapshot

    # Bounded by the period's dates rather than by "the most recent N rows":
    # a limit takes whatever exists, which for a past period is the wrong
    # days entirely.
    snaps = (await ctx.db.execute(
        select(TrafficSnapshot)
        .where(
            TrafficSnapshot.site_id == ctx.site.id,
            TrafficSnapshot.date >= ctx.period.start_iso,
            TrafficSnapshot.date <= ctx.period.end_iso,
        )
        .order_by(TrafficSnapshot.date.desc())
    )).scalars().all()

    countries: Counter[str] = Counter()
    for snap in snaps:
        for row in (snap.geo_countries or []):
            if isinstance(row, dict) and row.get("country"):
                countries[str(row["country"])] += int(row.get("sessions") or row.get("views") or 0)

    if not countries:
        section.unavailable = (
            f"No country breakdown recorded in the traffic snapshots for {ctx.period.label}."
        )
        return section

    total = sum(countries.values())
    top = countries.most_common(10)
    section.headline = (
        f"{top[0][0]} accounts for {_pct(top[0][1], total)} of recorded sessions."
    )
    section.metrics = [
        Metric("Markets recorded", len(countries), "distinct countries in the snapshots"),
        Metric("Largest market share", round(top[0][1] / total * 100, 1),
               f"{top[0][0]}'s share of recorded sessions", unit="%"),
    ]
    section.tables.append(Table(
        "Sessions by market",
        ("Country", "Sessions", "Share"),
        tuple((name, f"{count:,}", _pct(count, total)) for name, count in top),
        note=f"Aggregated from the last {len(snaps)} daily snapshots.",
    ))
    section.notes.append(
        "Aggregated from daily snapshots, so a visitor returning on several days is counted "
        "on each of them."
    )
    return section


__all__ = ["build_conversion", "build_markets", "build_metadata", "build_search"]
