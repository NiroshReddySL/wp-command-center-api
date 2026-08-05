"""Executive summary, roadmap and backlog.

These are built last and read only the findings the other sections produced,
so they can restate but never add. If a priority appears in the backlog it
appeared first as an evidenced finding somewhere above — a summary that can
introduce claims is how a report ends up asserting things its own body does
not support.

Sequencing is derived, not authored: severity decides urgency and effort
decides how much can run in parallel. Nobody is named as an owner, because
the data contains no such fact.
"""
from app.reports.models import Finding, Metric, Report, Section, Table

# Effort → the phase a workstream can realistically start in. Critical work
# starts immediately whatever its size; everything else queues behind it.
_EFFORT_PHASE = {"Small": 1, "Medium": 2, "Large": 3, "XL": 3}
_SEVERITY_PRIORITY = {"critical": "P0", "high": "P1", "medium": "P2", "opportunity": "P2"}

PHASES = (
    (1, "Weeks 1–4", "Stop the bleeding",
     "Anything exploitable or actively losing traffic, plus whatever is needed to trust "
     "the measurements."),
    (2, "Months 2–3", "Fix the causes",
     "Template-level and process-level repairs, so the same defects stop recurring."),
    (3, "Months 4–6", "Compound the gains",
     "Content and metadata work, which pays back slowly and only once the foundation holds."),
)


def build_executive(report: Report) -> Section:
    """A summary of what the sections already established."""
    findings = report.all_findings
    counts = report.severity_counts()
    sources = report.sources
    unavailable = [s for s in sources if not s.available]

    if not findings:
        headline = "No findings were raised against the data available for this period."
    else:
        worst = findings[0]
        headline = (
            f"{len(findings)} findings across {len([s for s in report.sections if not s.unavailable])} "
            f"sections; the most urgent is: {worst.title.lower()}."
        )

    section = Section(
        key="executive", number="00", title="Executive Summary", headline=headline,
        metrics=[
            Metric("Critical", counts["critical"], "findings needing action now"),
            Metric("High", counts["high"], "findings needing action this quarter"),
            Metric("Medium", counts["medium"], "findings worth scheduling"),
            Metric("Opportunities", counts["opportunity"],
                   "gains available without fixing a defect"),
        ],
    )

    if findings:
        section.tables.append(Table(
            "Findings, most urgent first",
            ("ID", "Finding", "Severity", "Effort"),
            tuple((f.id, f.title, f.severity.title(), f.effort) for f in findings),
            note="Every row is evidenced in the section it came from.",
        ))

    section.notes.append(
        f"Compiled from {len(sources) - len(unavailable)} of {len(sources)} data sources. "
        + (
            "Sources that were unavailable are named in the appendix, and the sections "
            "depending on them state that rather than reporting zero."
            if unavailable else
            "Every source was available for this report."
        )
    )
    return section


def build_roadmap(report: Report) -> Section:
    """Sequencing derived from the findings' own severity and effort."""
    findings = report.all_findings
    section = Section(
        key="roadmap", number="90", title="Suggested Sequence",
        headline="No work is outstanding.",
    )
    if not findings:
        return section

    def phase_of(f: Finding) -> int:
        # Critical work starts now regardless of size; the rest queues by effort.
        return 1 if f.severity == "critical" else _EFFORT_PHASE.get(f.effort, 2)

    buckets: dict[int, list[Finding]] = {1: [], 2: [], 3: []}
    for finding in findings:
        buckets[phase_of(finding)].append(finding)

    section.headline = (
        f"{len(findings)} findings sequenced across three phases by urgency and size."
    )
    section.tables.append(Table(
        "Phases",
        ("Phase", "Window", "Focus", "Items", "What it covers"),
        tuple(
            (f"Phase {n}", window, focus, str(len(buckets[n])),
             ", ".join(f.id for f in buckets[n]) or "—")
            for n, window, focus, _ in PHASES
        ),
        note="Windows are indicative. Sequence is derived from severity and effort, not "
             "from any estimate of your team's capacity.",
    ))
    for n, window, focus, description in PHASES:
        if buckets[n]:
            section.notes.append(f"Phase {n} ({window}) — {focus}. {description}")
    return section


def build_backlog(report: Report) -> Section:
    """The findings as a prioritised worklist."""
    findings = report.all_findings
    section = Section(
        key="backlog", number="91", title="Prioritised Backlog",
        headline="Nothing outstanding.",
    )
    if not findings:
        return section

    section.headline = f"{len(findings)} workstreams, prioritised by severity."
    section.tables.append(Table(
        "Backlog",
        ("Priority", "Workstream", "Evidence", "Effort", "Done when"),
        tuple(
            (
                _SEVERITY_PRIORITY.get(f.severity, "P2"),
                f.title,
                # Truncated on a word boundary so a table cell stays readable
                # while the full evidence remains in the section above.
                (f.evidence[:118].rsplit(" ", 1)[0] + "…") if len(f.evidence) > 120 else f.evidence,
                f.effort,
                f.measures[0] if f.measures else "—",
            )
            for f in findings
        ),
        note="P0 is exploitable or actively losing traffic; P1 needs this quarter; P2 is "
             "worth scheduling. No owners are assigned — the data does not contain that.",
    ))
    return section


__all__ = ["PHASES", "build_backlog", "build_executive", "build_roadmap"]
