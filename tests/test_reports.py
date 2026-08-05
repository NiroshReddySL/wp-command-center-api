"""Report generation — the accuracy contract.

A report is read by people who will act on it and cannot check it. So the
properties worth pinning are not "does it render" but "can it ever state
something it did not measure": every figure carries what it counted, an
unavailable source renders as a gap rather than a zero, and nothing in the
pipeline invents a number.
"""
import json
from datetime import UTC, datetime

import pytest

from app.reports.html import render_report
from app.reports.models import Finding, Metric, Report, Section, SourceStatus, Table


def _report(**over) -> Report:
    base = {
        "site_name": "Example",
        "site_url": "https://example.com",
        "period_start": "2026-07-08",
        "period_end": "2026-08-05",
        "generated_at": datetime(2026, 8, 5, tzinfo=UTC),
    }
    base.update(over)
    return Report(**base)


class TestMetric:
    def test_a_figure_carries_what_it_counted(self) -> None:
        # Without this a reader cannot tell whether two reports disagree or
        # are simply counting different things.
        m = Metric("Open findings", 387, "alerts in open or acknowledged state")
        assert m.basis
        assert m.is_known

    def test_an_unmeasurable_figure_is_none_not_zero(self) -> None:
        # Zero means "measured, and the answer was none". None means "we could
        # not look". Collapsing them is the failure this whole module exists
        # to prevent.
        assert Metric("Bounce rate", None, "mean of daily rates").is_known is False
        assert Metric("Bounce rate", 0, "mean of daily rates").is_known is True


class TestFinding:
    def test_severity_must_be_known(self) -> None:
        with pytest.raises(ValueError):
            Finding(id="X-01", title="t", severity="urgent", evidence="e", implication="i")

    def test_findings_rank_worst_first(self) -> None:
        section = Section(key="k", number="01", title="T", headline="h", findings=[
            Finding(id="A", title="a", severity="medium", evidence="e", implication="i"),
            Finding(id="B", title="b", severity="critical", evidence="e", implication="i"),
            Finding(id="C", title="c", severity="high", evidence="e", implication="i"),
        ])
        assert [f.id for f in section.sorted_findings()] == ["B", "C", "A"]


class TestSnapshot:
    """Reports are stored, not recomputed on view — a figure that moves after
    the report was sent is worse than no figure, because someone acted on the
    version they were given."""

    def test_serialises_to_json(self) -> None:
        report = _report(
            sources=[SourceStatus("ga4", "Google Analytics 4", True, "Authorised")],
            sections=[Section(
                key="s", number="01", title="T", headline="h",
                metrics=[Metric("Sessions", 6612, "sum of daily snapshots")],
                findings=[Finding(id="X-01", title="t", severity="high",
                                  evidence="e", implication="i",
                                  actions=("do it",), measures=("done",))],
                tables=[Table("Pages", ("Page", "Views"), (("/a", "10"),))],
            )],
        )
        # Must survive a real round-trip: tuples, datetimes and nested dicts.
        text = json.dumps(report.to_dict())
        back = json.loads(text)
        assert back["sections"][0]["metrics"][0]["basis"] == "sum of daily snapshots"
        assert back["sections"][0]["findings"][0]["actions"] == ["do it"]
        assert back["severity_counts"]["high"] == 1

    def test_counts_severities_across_every_section(self) -> None:
        report = _report(sections=[
            Section(key="a", number="01", title="A", headline="", findings=[
                Finding(id="A", title="a", severity="critical", evidence="e", implication="i")]),
            Section(key="b", number="02", title="B", headline="", findings=[
                Finding(id="B", title="b", severity="critical", evidence="e", implication="i")]),
        ])
        assert report.severity_counts()["critical"] == 2

    def test_unavailable_sources_are_reported(self) -> None:
        report = _report(sources=[
            SourceStatus("ga4", "GA4", False, "not authorised"),
            SourceStatus("psi", "PSI", True, "ok"),
        ])
        assert [s.key for s in report.unavailable_sources] == ["ga4"]


class TestHtmlExport:
    def test_is_self_contained(self) -> None:
        # It has to survive being emailed, archived and printed years later,
        # so it may not fetch anything at render time.
        out = render_report(_report().to_dict())
        assert "<script" not in out
        assert "src=" not in out and "@import" not in out

    def test_escapes_content(self) -> None:
        out = render_report(_report(site_name="<script>alert(1)</script>").to_dict())
        assert "<script>alert" not in out
        assert "&lt;script&gt;" in out

    def test_an_unavailable_section_states_the_reason(self) -> None:
        # The single most important rendering rule: a section that could not
        # be measured must not render as zeros.
        out = render_report(_report(sections=[Section(
            key="traffic", number="04", title="Traffic & Search", headline="",
            unavailable="Google authorisation missing since 2026-08-04",
        )]).to_dict())
        assert "Not available" in out
        assert "Google authorisation missing" in out
        assert ">0<" not in out

    def test_every_metric_renders_its_basis(self) -> None:
        out = render_report(_report(sections=[Section(
            key="s", number="01", title="T", headline="h",
            metrics=[Metric("Sessions", 6612, "sum of daily snapshots over 26 days")],
        )]).to_dict())
        assert "6,612" in out
        assert "sum of daily snapshots over 26 days" in out

    def test_missing_values_render_as_a_dash_not_a_zero(self) -> None:
        out = render_report(_report(sections=[Section(
            key="s", number="01", title="T", headline="h",
            metrics=[Metric("Bounce rate", None, "mean of daily rates", unit="%")],
        )]).to_dict())
        assert "—" in out

    def test_coverage_appendix_is_always_present(self) -> None:
        out = render_report(_report(sources=[
            SourceStatus("gsc", "Search Console", False, "not authorised", coverage=""),
        ]).to_dict())
        assert "could not measure" in out
        assert "Search Console" in out
        assert "not authorised" in out

    def test_renders_an_empty_report_without_failing(self) -> None:
        # A brand-new site has nothing to say yet; that must not be an error.
        out = render_report(_report().to_dict())
        assert "No findings" in out
