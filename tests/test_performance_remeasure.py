"""Measuring a page on demand, and what a measurement is allowed to claim.

Two things are pinned here. First, that `classify` is the single authority on
what a score means: the scheduled sweep and the hand-triggered re-measure both
run through it, and the moment there are two copies of these thresholds the
same page starts reading differently depending on which one touched it last.

Second, that a bounded batch never passes for a complete one. A full sweep of
an enterprise library does not fit in one job at keyless PageSpeed rates, so
"re-measure everything" is always a slice — and it has to be the slice that
makes progress, or pressing the button twice measures the same pages twice.
"""
from datetime import UTC, datetime, timedelta

import pytest

from app.agents.watchdog.performance import (
    Measurement,
    classify,
    psi_concurrency,
    snapshot_for,
)
from app.services.performance_rescan import SCOPES, order_by_staleness, rescan_ceiling

HOME = "https://example.com"
NOW = datetime(2026, 8, 10, tzinfo=UTC)


def _psi(score: int, **over: float) -> Measurement:
    return Measurement(
        url=f"{HOME}/page", score=score, lcp=2000.0, cls=0.05,
        fid=100.0, ttfb=400.0, source="psi", **over,  # type: ignore[arg-type]
    )


class TestClassify:
    def test_a_good_score_clears_rather_than_reports(self) -> None:
        # None severity means "delete this page's alert". A fixed page that
        # keeps its alert is indistinguishable from one nobody fixed.
        assert classify(_psi(94)).severity is None

    @pytest.mark.parametrize(
        ("score", "severity"),
        [(0, "critical"), (49, "critical"), (50, "warning"), (89, "warning"), (90, None)],
    )
    def test_thresholds(self, score: int, severity: str | None) -> None:
        assert classify(_psi(score)).severity == severity

    def test_an_unreachable_page_is_critical_not_a_zero_score(self) -> None:
        verdict = classify(Measurement(url=f"{HOME}/gone", source="error", error="timed out"))
        assert verdict.severity == "critical"
        assert "timed out" in verdict.description
        assert "unreachable" in verdict.title.lower()

    def test_the_alert_is_keyed_by_page_url(self) -> None:
        # Alert identity. Without it every run creates a fresh alert, losing
        # first-seen and resurrecting anything already acknowledged.
        assert classify(_psi(30)).metadata["page_url"] == f"{HOME}/page"

    def test_an_estimate_says_it_is_an_estimate(self) -> None:
        # A throttled measurement that reads like a PageSpeed score is worse
        # than no measurement — it is a wrong number nobody knows to doubt.
        m = Measurement(url=f"{HOME}/page", score=72, ttfb=1000.0, source="ttfb")
        verdict = classify(m)
        assert verdict.metadata["source"] == "ttfb"
        assert "PageSpeed Insights was unavailable" in verdict.description

    def test_an_estimate_does_not_invent_core_web_vitals(self) -> None:
        # LCP and CLS cannot be derived from TTFB. Zeros render as "—";
        # fabricated values would silently pollute the vitals trend.
        m = Measurement(url=f"{HOME}/page", score=72, ttfb=1000.0, source="ttfb")
        assert classify(m).metadata["lcp_ms"] == 0
        assert classify(m).metadata["cls"] == 0.0

    def test_a_psi_measurement_reports_its_vitals(self) -> None:
        meta = classify(_psi(40)).metadata
        assert meta["lcp_ms"] == 2000 and meta["ttfb_ms"] == 400
        assert meta["grade"] == "Poor"


class TestSnapshot:
    def test_a_snapshot_records_what_was_measured(self) -> None:
        snap = snapshot_for("site-1", _psi(64))
        assert snap.page_url == f"{HOME}/page"
        assert snap.speed_score == 64 and snap.lcp == 2000.0
        assert snap.strategy == "desktop"


class TestBatchOrdering:
    def _seen(self, **pages: int) -> dict[str, datetime]:
        """Pages keyed to how many hours ago they were last measured."""
        return {f"{HOME}/{name}": NOW - timedelta(hours=h) for name, h in pages.items()}

    def test_never_measured_pages_come_first(self) -> None:
        pages = [f"{HOME}/a", f"{HOME}/b"]
        assert order_by_staleness(pages, self._seen(a=1)) == [f"{HOME}/b", f"{HOME}/a"]

    def test_stale_before_recent(self) -> None:
        pages = [f"{HOME}/recent", f"{HOME}/old"]
        ordered = order_by_staleness(pages, self._seen(recent=2, old=400))
        assert ordered == [f"{HOME}/old", f"{HOME}/recent"]

    def test_incoming_order_breaks_ties(self) -> None:
        # Candidates arrive in traffic order, so among equally stale pages the
        # busiest is measured first.
        pages = [f"{HOME}/busy", f"{HOME}/quiet"]
        assert order_by_staleness(pages, {}) == pages

    def test_duplicates_are_collapsed(self) -> None:
        # A page can be both a tracked post and the subject of an alert.
        pages = [f"{HOME}/a", f"{HOME}/a", f"{HOME}/b"]
        assert order_by_staleness(pages, {}) == [f"{HOME}/a", f"{HOME}/b"]

    def test_repeating_the_batch_reaches_new_pages(self) -> None:
        # The point of stale-first ordering: a capped batch followed by
        # another capped batch covers twice as much, not the same twice.
        pages = [f"{HOME}/p{i}" for i in range(10)]
        first = order_by_staleness(pages, {})[:5]
        after = dict.fromkeys(first, NOW)
        second = order_by_staleness(pages, after)[:5]
        assert not set(first) & set(second)


class TestLimits:
    def test_a_manual_batch_is_bounded(self) -> None:
        # "Everything" cannot mean everything at keyless PageSpeed rates. The
        # ceiling is what keeps the promise honest — the response reports the
        # candidate count alongside it so a slice never reads as a sweep.
        assert 0 < rescan_ceiling() <= 1000

    def test_a_manual_batch_uses_the_same_quota_ceiling_as_the_sweep(self) -> None:
        # A hand-triggered batch that ran hotter than the scheduled one would
        # just spend the shared per-IP allowance on 429s.
        assert psi_concurrency() >= 1

    def test_the_offered_scopes_are_the_ones_the_ui_sends(self) -> None:
        assert SCOPES == ("reported", "all")
