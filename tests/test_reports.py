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

from app.reports.context import ReportContext
from app.reports.html import render_report
from app.reports.models import Finding, Metric, Report, Section, SourceStatus, Table
from app.services.content_rescan import CONCURRENCY, MAX_BATCH, BulkProgress


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


class TestSearchArithmetic:
    """The two ways a search report misleads without lying.

    A mean of daily CTRs weights a quiet Sunday the same as a busy Tuesday,
    so it reads differently from the same data. And an average position that
    ignores impressions describes a site nobody visited.
    """

    @staticmethod
    def _ctx(daily):
        ctx = ReportContext(db=None, site=None, sources=[])  # type: ignore[arg-type]
        ctx.search_daily = daily
        return ctx

    def test_ctr_comes_from_period_totals_not_a_mean_of_days(self) -> None:
        # One huge day at a poor rate, one tiny day at a great rate.
        daily = [
            {"clicks": 10, "impressions": 10_000, "position": 20.0},
            {"clicks": 5, "impressions": 10, "position": 2.0},
        ]
        totals = self._ctx(daily).search_totals
        assert totals is not None
        # Totals: 15 / 10,010 = 0.15%. A mean of daily rates would be 25.05%.
        assert round(totals["ctr"], 2) == 0.15
        assert totals["clicks"] == 15
        assert totals["impressions"] == 10_010

    def test_position_is_weighted_by_impressions(self) -> None:
        daily = [
            {"clicks": 0, "impressions": 10_000, "position": 20.0},
            {"clicks": 0, "impressions": 10, "position": 2.0},
        ]
        totals = self._ctx(daily).search_totals
        assert totals is not None
        # An unweighted mean would be 11.0 — a position the site never held.
        assert 19.9 < totals["position"] < 20.0

    def test_no_data_is_none_rather_than_zeroes(self) -> None:
        # Zero impressions and "we could not ask" must stay distinguishable.
        assert self._ctx([]).search_totals is None
        assert self._ctx(None).search_totals is None

    def test_a_period_with_no_impressions_does_not_divide_by_zero(self) -> None:
        totals = self._ctx([{"clicks": 0, "impressions": 0, "position": 0.0}]).search_totals
        assert totals is not None
        assert totals["ctr"] == 0.0 and totals["position"] == 0.0


class TestBulkRescanProgress:
    """A batch of fifty takes minutes — each page costs a WordPress fetch, a
    live page fetch and an AI call. Progress therefore has to be observable
    while it runs, and a page that vanished has to stay distinguishable from
    one that failed."""

    def test_starts_empty_and_not_running(self) -> None:
        p = BulkProgress()
        assert p.running is False and p.total == 0 and p.failures == []

    def test_outcomes_are_counted_separately(self) -> None:
        # "removed" is a successful outcome — WordPress confirmed the page is
        # gone — and must not inflate the failure count.
        p = BulkProgress(total=3, done=1, failed=1, removed=1)
        assert p.done + p.failed + p.removed == p.total
        assert p.failed == 1

    def test_serialises_for_the_status_endpoint(self) -> None:
        data = BulkProgress(total=2, done=2, running=False).as_dict()
        assert json.dumps(data)
        assert data["total"] == 2 and data["running"] is False

    def test_batch_size_is_bounded(self) -> None:
        # Unbounded, one click could queue every page on the site against
        # someone's production WordPress.
        assert 0 < MAX_BATCH <= 500

    def test_concurrency_is_deliberately_low(self) -> None:
        # Every unit is another simultaneous request to the customer's site.
        assert 1 <= CONCURRENCY <= 5
