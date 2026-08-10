"""The shapes a report is made of.

The organising rule: **a figure may not appear in a report without saying what
it counted and how much of the estate it covers.** "662 findings" is not a
fact anyone can act on; "662 open or acknowledged alerts across 3 sites, as of
5 August" is. Every number here therefore travels with its `basis`, and every
section with the coverage of the data behind it.

The second rule follows from the first: nothing in a report may be generated
prose. A language model can phrase a sentence around a number that was already
computed, but it must never be the thing that produces the number — that is
the difference between a report and a plausible-sounding document.
"""
from dataclasses import asdict, dataclass, field
from datetime import datetime
from typing import Any

# Ordered worst-first; used for ranking findings and colouring badges.
SEVERITIES = ("critical", "high", "medium", "opportunity")
SEVERITY_RANK = {s: i for i, s in enumerate(SEVERITIES)}


@dataclass(frozen=True)
class Metric:
    """One headline figure, with the sentence that makes it checkable."""

    label: str
    value: int | float | str | None
    # What was actually counted — "unique HTML URLs", "open + acknowledged
    # alerts", "components with a successful WPScan lookup". Without this a
    # reader cannot tell whether two reports disagree or merely count
    # different things.
    basis: str
    sub: str = ""
    unit: str = ""
    # None when the figure could not be computed at all, which renders as an
    # explicit gap rather than as zero.
    as_of: datetime | None = None

    @property
    def is_known(self) -> bool:
        return self.value is not None


@dataclass(frozen=True)
class SourceStatus:
    """Whether a data source could be used, and how completely.

    Probed before anything is claimed from it. A section whose source is
    unavailable renders as a stated gap — "no search data, Google
    authorisation missing since 4 August" — never as zeros, which read as
    "measured, and the answer was none".
    """

    key: str
    label: str
    available: bool
    # Why it is unavailable, or how fresh it is when it is.
    detail: str
    as_of: datetime | None = None
    # "37 of 44 components" — partial availability is the normal case and
    # has to be visible, not rounded up to "available".
    coverage: str = ""


@dataclass(frozen=True)
class Finding:
    """One conclusion, in the anatomy an executive can act on."""

    id: str
    title: str
    severity: str
    # Every clause here must trace to a Metric or a counted query. This is
    # the field that makes the report auditable.
    evidence: str
    implication: str
    actions: tuple[str, ...] = ()
    measures: tuple[str, ...] = ()
    effort: str = "Medium"

    def __post_init__(self) -> None:
        if self.severity not in SEVERITY_RANK:
            raise ValueError(f"unknown severity: {self.severity}")


@dataclass(frozen=True)
class Table:
    """Supporting detail. Rows are pre-formatted strings so the renderer
    never has to decide how a number should read."""

    title: str
    columns: tuple[str, ...]
    rows: tuple[tuple[str, ...], ...]
    note: str = ""


@dataclass
class Section:
    key: str
    number: str
    title: str
    # A sentence stating what the data shows — written from the computed
    # figures, not about them in the abstract.
    headline: str
    metrics: list[Metric] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    tables: list[Table] = field(default_factory=list)
    # Caveats specific to this section's data, e.g. "2 of 4 PageSpeed scores
    # are TTFB estimates and are excluded from the average".
    notes: list[str] = field(default_factory=list)
    # Set when the section could not be produced; the report renders the
    # reason in place of the content.
    unavailable: str | None = None

    def sorted_findings(self) -> list[Finding]:
        return sorted(self.findings, key=lambda f: SEVERITY_RANK[f.severity])


@dataclass
class Report:
    site_name: str
    site_url: str
    period_start: str
    period_end: str
    generated_at: datetime
    # Rendered once at build time so the stored snapshot carries the wording
    # the report was issued with, rather than leaving each renderer to format
    # the dates its own way and drift apart.
    period_label: str = ""
    period_days: int = 0
    # Which sections cover the period and which describe the site now. Without
    # it a reader reasonably assumes the whole document is period-scoped.
    scope_note: str = ""
    sources: list[SourceStatus] = field(default_factory=list)
    sections: list[Section] = field(default_factory=list)

    @property
    def all_findings(self) -> list[Finding]:
        return sorted(
            (f for s in self.sections for f in s.findings),
            key=lambda f: SEVERITY_RANK[f.severity],
        )

    def severity_counts(self) -> dict[str, int]:
        counts = dict.fromkeys(SEVERITIES, 0)
        for finding in self.all_findings:
            counts[finding.severity] += 1
        return counts

    @property
    def unavailable_sources(self) -> list[SourceStatus]:
        return [s for s in self.sources if not s.available]

    def to_dict(self) -> dict[str, Any]:
        """Frozen snapshot. Stored verbatim so a report sent last week still
        shows last week's numbers — a figure that moves after the fact is
        worse than no figure."""
        return {
            "site_name": self.site_name,
            "site_url": self.site_url,
            "period_start": self.period_start,
            "period_end": self.period_end,
            "period_label": self.period_label or f"{self.period_start} → {self.period_end}",
            "period_days": self.period_days,
            "scope_note": self.scope_note,
            "generated_at": self.generated_at.isoformat(),
            "severity_counts": self.severity_counts(),
            "sources": [_clean(asdict(s)) for s in self.sources],
            "sections": [
                {
                    **_clean(asdict(section)),
                    "findings": [_clean(asdict(f)) for f in section.sorted_findings()],
                }
                for section in self.sections
            ],
        }


def _clean(data: dict[str, Any]) -> dict[str, Any]:
    """datetimes to ISO strings, tuples to lists — JSON-safe, recursively."""
    out: dict[str, Any] = {}
    for key, value in data.items():
        if isinstance(value, datetime):
            out[key] = value.isoformat()
        elif isinstance(value, tuple):
            out[key] = [_clean(v) if isinstance(v, dict) else list(v) if isinstance(v, tuple) else v for v in value]
        elif isinstance(value, list):
            out[key] = [_clean(v) if isinstance(v, dict) else v for v in value]
        elif isinstance(value, dict):
            out[key] = _clean(value)
        else:
            out[key] = value
    return out


__all__ = [
    "SEVERITIES", "SEVERITY_RANK",
    "Finding", "Metric", "Report", "Section", "SourceStatus", "Table",
]
