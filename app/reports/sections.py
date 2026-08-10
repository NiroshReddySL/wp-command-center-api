"""The four sections, built from measured data only.

Nothing here calls a language model. Every figure is the result of a query,
and every finding is a rule over those figures — so the same site on the same
day produces the same report, and any line can be traced back to rows in the
database. That determinism is the whole basis of the accuracy claim: prose
generated about numbers can drift, prose assembled around them cannot.

Where a source is unavailable the section says so and stops. It never
substitutes zero, because "measured, and the answer was none" and "we could
not look" are opposite conclusions that happen to render the same.
"""
from collections import Counter
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import (
    Alert,
    ContentPost,
    PerformanceSnapshot,
    PluginAudit,
    TrafficSnapshot,
)
from app.reports.analysed import ANALYSED
from app.reports.models import Finding, Metric, Section, SourceStatus, Table
from app.reports.period import Period

_CWV_GOOD_SCORE = 90
_CWV_POOR_SCORE = 50
_THIN_WORDS = 300
_STALE_DAYS = 365


def _pct(part: int, whole: int) -> str:
    return f"{(part / whole * 100):.1f}%" if whole else "—"


def _source(sources: list[SourceStatus], key: str) -> SourceStatus | None:
    return next((s for s in sources if s.key == key), None)


# ── 01 Security & components ─────────────────────────────────────────────────

async def build_security(
    db: AsyncSession, site_id: str, sources: list[SourceStatus], period: Period
) -> Section:
    rows = (await db.execute(
        select(PluginAudit).where(PluginAudit.site_id == site_id)
    )).scalars().all()

    section = Section(
        key="security", number="07", title="Security & Components",
        headline="No plugins or themes are tracked for this site.",
    )
    if not rows:
        section.unavailable = (
            "No components tracked. Connect an Application Password to read them "
            "from WordPress, or record them by hand."
        )
        return section

    scan = _source(sources, "components")
    plugins = [r for r in rows if r.component_type == "plugin"]
    themes = [r for r in rows if r.component_type == "theme"]
    vulnerable = [r for r in rows if r.risk_level == "critical"]
    outdated = [r for r in rows if r.risk_level == "high"]
    untracked = [r for r in rows if r.latest_source == "unknown"]
    cve_total = sum(
        len((r.vulnerability_details or {}).get("vulnerabilities") or []) for r in vulnerable
    )
    manual = [r for r in rows if r.source == "manual"]

    section.headline = (
        f"{len(vulnerable)} of {len(rows)} tracked components carry known vulnerabilities "
        f"affecting the installed version."
        if vulnerable else
        f"No known vulnerabilities affect the {len(rows)} tracked components."
    )
    section.metrics = [
        Metric("Components tracked", len(rows),
               f"{len(plugins)} plugins, {len(themes)} themes", unit=""),
        Metric("Vulnerable", len(vulnerable),
               "components with at least one CVE affecting the installed version",
               sub=f"{cve_total} vulnerabilities in total"),
        Metric("Update available", len(outdated),
               "installed version older than the latest published release"),
        Metric("Not verifiable", len(untracked),
               "no published latest version could be resolved",
               sub="premium or custom components"),
    ]

    if scan and scan.coverage:
        section.notes.append(
            f"Vulnerability coverage: {scan.coverage}. Components without a result are "
            "reported as unknown, not as clean — an unchecked component is not a safe one."
        )
    if manual:
        section.notes.append(
            f"{len(manual)} of {len(rows)} components were recorded by hand rather than read "
            "from WordPress, so the inventory is only as complete as what was entered."
        )

    if vulnerable:
        worst = sorted(
            vulnerable,
            key=lambda r: len((r.vulnerability_details or {}).get("vulnerabilities") or []),
            reverse=True,
        )
        section.tables.append(Table(
            "Components with known vulnerabilities",
            ("Component", "Type", "Installed", "CVEs affecting this version"),
            tuple(
                (r.plugin_name or r.plugin_slug, r.component_type, r.installed_version,
                 str(len((r.vulnerability_details or {}).get("vulnerabilities") or [])))
                for r in worst[:15]
            ),
            note="Filtered to the installed version — a vulnerability patched in a release "
                 "you already run is excluded.",
        ))
        top = worst[0]
        top_cves = len((top.vulnerability_details or {}).get("vulnerabilities") or [])
        section.findings.append(Finding(
            id="SEC-01",
            title="Components with known vulnerabilities are in production",
            severity="critical",
            evidence=(
                f"{len(vulnerable)} of {len(rows)} tracked components carry {cve_total} "
                f"vulnerabilities affecting their installed versions. The largest single "
                f"exposure is {top.plugin_name or top.plugin_slug} "
                f"{top.installed_version} with {top_cves}."
            ),
            implication=(
                "Each of these is a published, catalogued weakness against a version this "
                "site is running now. Disclosure precedes exploitation, so the window is "
                "already open."
            ),
            actions=(
                "Patch the components above, worst first.",
                "Where no patch exists, assess whether the component can be removed.",
                "Confirm the fix by re-running the audit rather than assuming the update applied.",
            ),
            measures=("Zero components with vulnerabilities affecting installed versions",
                      "Every component resolvable to a published latest version"),
            effort="Medium",
        ))

    if outdated:
        section.findings.append(Finding(
            id="SEC-02", title="Components are running behind their published releases",
            severity="high",
            evidence=(
                f"{len(outdated)} of {len(rows)} components ({_pct(len(outdated), len(rows))}) "
                f"are older than their latest published release."
            ),
            implication=(
                "Version drift is where future vulnerabilities land: a component already "
                "behind has no path to a patch without an update process."
            ),
            actions=("Establish a regular update cadence rather than updating on incident.",
                     "Prioritise components that are both outdated and internet-facing."),
            measures=("Median version lag reduced", "No component more than one minor release behind"),
            effort="Medium",
        ))

    if untracked:
        names = ", ".join((r.plugin_name or r.plugin_slug) for r in untracked[:5])
        section.findings.append(Finding(
            id="SEC-03", title="Some components cannot be checked for updates at all",
            severity="medium",
            evidence=(
                f"{len(untracked)} components have no resolvable published version, "
                f"including {names}. These are premium or custom builds that the "
                f"WordPress.org directory does not carry."
            ),
            implication=(
                "Without a known latest version these cannot be reported as current or "
                "outdated. They are reported as unverifiable, which is accurate but means "
                "they are outside automated update monitoring."
            ),
            actions=("Record the vendor's current version against each, so drift becomes visible.",
                     "Subscribe to vendor release notifications for anything business-critical."),
            measures=("Every component has a known latest version, from the directory or recorded by hand",),
            effort="Small",
        ))

    return section


# ── 02 Site health & technical debt ──────────────────────────────────────────

async def build_health(
    db: AsyncSession, site_id: str, sources: list[SourceStatus], period: Period
) -> Section:
    alerts = (await db.execute(
        select(Alert).where(
            Alert.site_id == site_id, Alert.status.in_(["open", "acknowledged"])
        )
    )).scalars().all()

    by_severity = Counter(a.severity for a in alerts)
    by_type = Counter(a.type for a in alerts)
    broken = by_type.get("broken_link", 0)

    section = Section(
        key="health", number="08", title="Site Health & Technical Debt",
        headline=(
            f"{len(alerts)} findings are open or acknowledged across all monitoring agents."
            if alerts else "No open findings across any monitoring agent."
        ),
        metrics=[
            Metric("Open findings", len(alerts), "alerts in open or acknowledged state"),
            Metric("Critical", by_severity.get("critical", 0), "severity as classified by the detecting agent"),
            Metric("Broken links", broken, "internal and external links returning an error"),
        ],
    )

    psi = _source(sources, "psi")
    # Scoped to the reporting period like any other measurement series: a
    # report for March that quoted this month's PageSpeed scores would be
    # stating a number the period never produced.
    snaps = (await db.execute(
        select(PerformanceSnapshot)
        .where(
            PerformanceSnapshot.site_id == site_id,
            PerformanceSnapshot.snapshot_at >= period.start_dt,
            PerformanceSnapshot.snapshot_at < period.end_dt,
        )
        .order_by(PerformanceSnapshot.snapshot_at.desc())
    )).scalars().all()

    # A TTFB-estimated row carries no Core Web Vitals. Averaging it with real
    # Lighthouse runs would produce a number describing neither.
    measured = [s for s in snaps if s.lcp or s.cls]
    if measured:
        latest_by_page: dict[str, PerformanceSnapshot] = {}
        for snap in measured:
            latest_by_page.setdefault(snap.page_url, snap)
        pages = list(latest_by_page.values())
        ranked = sorted(p.speed_score for p in pages)
        median_score = ranked[len(ranked) // 2]
        poor = [p for p in pages if p.speed_score < _CWV_POOR_SCORE]
        section.metrics.append(
            Metric("Median PageSpeed", median_score,
                   f"median of the most recent Lighthouse score for each of {len(pages)} "
                   f"pages measured during {period.label}",
                   sub=f"{len(pages)} page(s) measured")
        )
        section.tables.append(Table(
            "Most recent PageSpeed result per page",
            ("Page", "Score", "LCP", "CLS"),
            tuple(
                (p.page_url, str(p.speed_score),
                 f"{p.lcp / 1000:.1f}s" if p.lcp else "—",
                 f"{p.cls:.2f}" if p.cls is not None else "—")
                for p in sorted(pages, key=lambda p: p.speed_score)[:10]
            ),
        ))
        if poor:
            section.findings.append(Finding(
                id="TECH-02", title="Pages are scoring poorly on measured performance",
                severity="high",
                evidence=(
                    f"{len(poor)} of {len(pages)} measured pages score below "
                    f"{_CWV_POOR_SCORE}/100 on Lighthouse."
                ),
                implication=("Slow pages lose visitors before content renders, and page "
                             "experience is a ranking input."),
                actions=("Address the specific Core Web Vitals failing on each page.",
                         "Fix shared templates rather than individual pages where the cause is common."),
                measures=("All measured pages above 50", "Core Web Vitals passing on priority pages"),
                effort="Large",
            ))
    if psi and psi.coverage and "of" in psi.coverage:
        section.notes.append(
            f"PageSpeed coverage: {psi.coverage}. TTFB estimates are excluded from every "
            "score above; mixing them with Lighthouse runs would describe neither."
        )
    # The two halves of this section answer different questions, and saying
    # which is which is the difference between a figure and a guess.
    section.notes.append(
        f"Speed figures cover pages measured during {period.label}. Finding counts are "
        "the site's current state — an alert is open or it is not, and there is no "
        "stored history that could reconstruct what was open during a past period."
    )

    if broken:
        section.findings.append(Finding(
            id="TECH-01", title="Broken links are reachable by visitors",
            severity="critical" if broken >= 20 else "high",
            evidence=f"{broken} links currently resolve to an error, out of {len(alerts)} open findings.",
            implication=("Broken links waste crawl budget, strand visitors and lose whatever "
                         "authority the destination once carried."),
            actions=("Fix or remove the source links rather than only redirecting destinations.",
                     "Where a template generates the pattern, repair the template."),
            measures=("Broken internal links reduced by 90%", "No broken links on commercial pages"),
            effort="Medium",
        ))

    if by_severity.get("critical", 0):
        section.tables.append(Table(
            "Open findings by type",
            ("Finding type", "Count", "Share"),
            tuple(
                (t.replace("_", " ").title(), str(c), _pct(c, len(alerts)))
                for t, c in by_type.most_common(10)
            ),
        ))

    return section


# ── 03 Content portfolio ─────────────────────────────────────────────────────

async def build_content(
    db: AsyncSession, site_id: str, sources: list[SourceStatus], period: Period
) -> Section:
    source = _source(sources, "content")
    section = Section(
        key="content", number="05", title="Content Portfolio",
        headline="No content has been synced for this site.",
    )
    if source and not source.available:
        section.unavailable = source.detail
        return section

    posts = (await db.execute(
        select(ContentPost).where(ContentPost.site_id == site_id, ANALYSED)
    )).scalars().all()
    if not posts:
        section.unavailable = "No pages have been scored yet."
        return section

    scores = sorted(p.health_score for p in posts)
    median = scores[len(scores) // 2]
    thin = [p for p in posts if 0 < p.word_count < _THIN_WORDS]
    cutoff = datetime.now(UTC) - timedelta(days=_STALE_DAYS)
    stale = [p for p in posts if p.wp_modified_at and p.wp_modified_at < cutoff]
    weak = [p for p in posts if p.health_score < 50]

    issue_counts: Counter[str] = Counter()
    for post in posts:
        for issue in (post.issues or []):
            issue_counts[str(issue)] += 1

    section.headline = (
        f"{len(posts)} pages scored, with a median health of {median}/100."
    )
    section.metrics = [
        Metric("Pages scored", len(posts), "pages with a computed health score"),
        Metric("Median health", median, "median of per-page health scores", unit="/100"),
        Metric("Below 50", len(weak), "pages scoring under 50/100",
               sub=_pct(len(weak), len(posts))),
        Metric("Thin", len(thin), f"pages under {_THIN_WORDS} words",
               sub=_pct(len(thin), len(posts))),
    ]
    if source and source.coverage:
        section.notes.append(
            f"Scoring coverage: {source.coverage}. Unscored pages are excluded from every "
            "figure above rather than counted as healthy."
        )
    section.notes.append(
        "Health scores are rule-based — word count, headings, metadata, links, images and "
        "freshness. They are not a judgement of whether the writing is any good."
    )

    if issue_counts:
        section.tables.append(Table(
            "Most common content issues",
            ("Issue", "Pages affected", "Share of scored pages"),
            tuple((issue, str(count), _pct(count, len(posts)))
                  for issue, count in issue_counts.most_common(10)),
        ))

    if weak:
        section.findings.append(Finding(
            id="CONT-01", title="A material share of the library scores below half marks",
            severity="high" if len(weak) / len(posts) > 0.25 else "medium",
            evidence=(
                f"{len(weak)} of {len(posts)} scored pages ({_pct(len(weak), len(posts))}) "
                f"score below 50/100. The median across the library is {median}/100."
            ),
            implication=("Low-scoring pages consume crawl budget and dilute topical authority "
                         "while contributing little."),
            actions=("Triage into improve, consolidate or retire — not all of it deserves rescue.",
                     "Fix issues that recur across many pages at the template level."),
            measures=("Median health score improved", "Pages below 50 reduced by half"),
            effort="Large",
        ))

    if stale:
        section.findings.append(Finding(
            id="CONT-02", title="Content has not been revisited in over a year",
            severity="medium",
            evidence=(
                f"{len(stale)} of {len(posts)} pages ({_pct(len(stale), len(posts))}) were last "
                f"modified more than {_STALE_DAYS} days ago."
            ),
            implication=("Ageing content drifts out of accuracy, and freshness is a ranking "
                         "input in fast-moving topics."),
            actions=("Refresh the stale pages that still earn traffic; retire those that do not.",
                     "Set a review cadence by content type rather than reviewing ad hoc."),
            measures=("Stale share reduced", "Every trafficked page reviewed within 12 months"),
            effort="Large",
        ))

    return section


# ── 02 Traffic trend ──────────────────────────────────────────────────────

async def build_traffic(
    db: AsyncSession, site_id: str, sources: list[SourceStatus], period: Period
) -> Section:
    section = Section(
        key="traffic", number="02", title="Traffic Trend",
        headline="No traffic data is available for this period.",
    )
    traffic_source = _source(sources, "traffic")
    ga4 = _source(sources, "ga4")

    if traffic_source and not traffic_source.available:
        reason = traffic_source.detail
        if ga4 and not ga4.available:
            reason = f"{ga4.detail}. {reason}"
        section.unavailable = reason
        return section

    snaps = (await db.execute(
        select(TrafficSnapshot)
        .where(
            TrafficSnapshot.site_id == site_id,
            TrafficSnapshot.date >= period.start_iso,
            TrafficSnapshot.date <= period.end_iso,
        )
        .order_by(TrafficSnapshot.date)
    )).scalars().all()
    if not snaps:
        section.unavailable = f"No traffic snapshots recorded between {period.label}."
        return section

    sessions = sum(s.sessions for s in snaps)
    users = sum(s.users for s in snaps)
    views = sum(s.pageviews for s in snaps)
    bounced = [s.bounce_rate for s in snaps if s.bounce_rate]
    days = len(snaps)

    section.headline = (
        f"{sessions:,} sessions across {days} days of recorded traffic."
    )
    section.metrics = [
        Metric("Sessions", sessions, f"sum of daily snapshots over {days} recorded days"),
        Metric("Users", users, f"sum of daily active users over {days} days",
               sub="daily figures summed — repeat visitors counted once per day"),
        Metric("Pageviews", views, f"sum of daily pageviews over {days} days"),
        Metric("Bounce rate",
               round(sum(bounced) / len(bounced), 1) if bounced else None,
               "mean of daily bounce rates", unit="%"),
    ]
    section.notes.append(
        "Daily figures are summed, so a visitor returning on three days counts three times. "
        f"This is not the same as a deduplicated {period.days}-day user count and should not "
        "be compared with one."
    )
    shortfall = period.shortfall_note(days)
    if shortfall:
        section.notes.append(shortfall)

    pages: Counter[str] = Counter()
    for snap in snaps:
        for entry in (snap.top_pages or []):
            if isinstance(entry, dict) and entry.get("url"):
                pages[str(entry["url"])] += int(entry.get("views") or 0)
    if pages:
        section.tables.append(Table(
            "Most-viewed pages",
            ("Page", "Views"),
            tuple((url, f"{count:,}") for url, count in pages.most_common(10)),
            note=f"Aggregated from the per-day top-page lists across {days} days.",
        ))

    if days >= 14:
        half = days // 2
        first = sum(s.sessions for s in snaps[:half])
        second = sum(s.sessions for s in snaps[half:half * 2])
        if first and second < first * 0.8:
            section.findings.append(Finding(
                id="TRAF-01", title="Sessions have fallen against the preceding period",
                severity="high",
                evidence=(
                    f"Sessions fell from {first:,} in the first {half} recorded days to "
                    f"{second:,} in the following {half} — a drop of "
                    f"{(1 - second / first) * 100:.0f}%."
                ),
                implication=("A sustained decline compounds; the cause is cheaper to find "
                             "while the change is recent."),
                actions=("Compare the drop against the pages that lost the most views.",
                         "Rule out tracking changes before assuming a demand or ranking change."),
                measures=("Sessions recovered to the prior baseline",),
                effort="Medium",
            ))

    return section


BUILDERS = (build_security, build_health, build_content, build_traffic)

__all__ = ["BUILDERS", "build_content", "build_health", "build_security", "build_traffic"]
