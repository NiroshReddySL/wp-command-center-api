"""The window a report covers, and what it is allowed to claim about it.

A report that says "1–31 March" across the top and computes its figures from
the last 28 days is not slightly wrong, it is unfalsifiable — the heading and
the numbers disagree and nothing looks broken. So the period is one value
resolved once, and every period-scoped figure takes its dates from it.

The other half of the contract is what a period CANNOT cover. Which plugins
are outdated and how many alerts are open are facts about the site now; no
stored history could reconstruct them for a past month. Claiming otherwise
would be inventing a number, which is the one thing these reports must never
do — so the report states the distinction rather than leaving it to be
assumed.
"""
from datetime import UTC, date, datetime

import pytest
from fastapi import HTTPException

from app.reports.html import render_report
from app.reports.models import Report
from app.reports.period import DEFAULT_RANGE, Period

MARCH = Period(date(2026, 3, 1), date(2026, 3, 31))
NOW = datetime(2026, 8, 10, tzinfo=UTC)


class TestPeriodDates:
    def test_length_is_inclusive_of_both_ends(self) -> None:
        # 1 to 31 March is 31 days. An exclusive count silently drops a day
        # from every total computed against it.
        assert MARCH.days == 31

    def test_a_single_day_is_one_day(self) -> None:
        assert Period(date(2026, 3, 1), date(2026, 3, 1)).days == 1

    def test_the_end_bound_includes_the_whole_final_day(self) -> None:
        # `< end_dt` against a timestamp column: anything stamped at 23:59 on
        # the last day is inside the period, not after it.
        assert MARCH.end_dt == datetime(2026, 4, 1, tzinfo=UTC)
        assert MARCH.start_dt == datetime(2026, 3, 1, tzinfo=UTC)

    def test_iso_dates_are_what_the_apis_receive(self) -> None:
        assert (MARCH.start_iso, MARCH.end_iso) == ("2026-03-01", "2026-03-31")


class TestPeriodLabel:
    def test_a_span_inside_one_month_names_the_month_once(self) -> None:
        assert MARCH.label == "1 – 31 Mar 2026"

    def test_a_span_across_months_names_both(self) -> None:
        assert Period(date(2026, 7, 13), date(2026, 8, 10)).label == "13 Jul – 10 Aug 2026"

    def test_a_span_across_years_names_both_years(self) -> None:
        label = Period(date(2025, 12, 29), date(2026, 1, 4)).label
        assert label == "29 Dec 2025 – 04 Jan 2026"

    def test_a_single_day_reads_as_a_date(self) -> None:
        assert Period(date(2026, 3, 1), date(2026, 3, 1)).label == "01 Mar 2026"


class TestPeriodFromRequest:
    def test_presets_resolve_the_same_way_as_every_other_range_control(self) -> None:
        # Shared resolution is the point: "last 28 days" in a report and on a
        # dashboard have to mean the same 28 days or comparing them is
        # meaningless.
        assert Period.from_request("28d", None, None).days == 28
        assert Period.from_request("7d", None, None).days == 7

    def test_a_custom_range_is_taken_literally(self) -> None:
        period = Period.from_request("custom", "2026-03-01", "2026-03-31")
        assert (period.start_iso, period.end_iso) == ("2026-03-01", "2026-03-31")

    def test_a_custom_range_missing_its_dates_is_refused(self) -> None:
        # Substituting a default here would produce a report for a period
        # nobody asked for, titled as though they had.
        with pytest.raises(HTTPException) as exc:
            Period.from_request("custom", "2026-03-01", None)
        assert exc.value.status_code == 422

    def test_an_unknown_range_is_refused(self) -> None:
        with pytest.raises(HTTPException):
            Period.from_request("last-fortnight", None, None)

    def test_the_default_is_a_known_preset(self) -> None:
        assert Period.from_request(DEFAULT_RANGE, None, None).days == 28


class TestHonesty:
    def test_a_closed_period_is_historical(self) -> None:
        assert MARCH.is_historical
        assert not Period.last_days(28).is_historical

    def test_partial_data_is_declared_not_silently_totalled(self) -> None:
        # A 31-day total built from 9 days of data is not a period total.
        # Reporting it without saying so reads as a collapse in traffic.
        note = MARCH.shortfall_note(9)
        assert note and "9 of the 31" in note

    def test_complete_data_needs_no_caveat(self) -> None:
        assert MARCH.shortfall_note(31) is None
        assert MARCH.shortfall_note(40) is None

    def test_the_scope_note_separates_period_from_current_state(self) -> None:
        note = MARCH.scope_note(NOW)
        assert "1 – 31 Mar 2026" in note
        assert "current state" in note
        # It must name the generation date, since that is what the
        # state-based sections are actually as-of.
        assert "10 Aug 2026" in note


class TestStoredSnapshot:
    def _report(self, **over) -> Report:
        base = {
            "site_name": "Example",
            "site_url": "https://example.com",
            "period_start": MARCH.start_iso,
            "period_end": MARCH.end_iso,
            "period_label": MARCH.label,
            "period_days": MARCH.days,
            "scope_note": MARCH.scope_note(NOW),
            "generated_at": NOW,
        }
        base.update(over)
        return Report(**base)

    def test_the_period_is_frozen_into_the_snapshot(self) -> None:
        # Stored, not recomputed: a report reopened in six months must still
        # say which period it covered, worded as it was issued.
        data = self._report().to_dict()
        assert data["period_label"] == "1 – 31 Mar 2026"
        assert data["period_days"] == 31
        assert "current state" in data["scope_note"]

    def test_an_older_report_without_a_label_still_renders_one(self) -> None:
        # Reports generated before the period picker existed have no label
        # stored; they must fall back to their dates, not to an empty string.
        data = Report(
            site_name="Example", site_url="https://example.com",
            period_start="2026-07-08", period_end="2026-08-05", generated_at=NOW,
        ).to_dict()
        assert data["period_label"] == "2026-07-08 → 2026-08-05"


class TestPrintedCover:
    def _html(self, **over) -> str:
        data = {
            "site_name": "Example", "site_url": "https://example.com",
            "period_start": MARCH.start_iso, "period_end": MARCH.end_iso,
            "period_label": MARCH.label, "period_days": MARCH.days,
            "scope_note": MARCH.scope_note(NOW),
            "generated_at": NOW.isoformat(),
            "severity_counts": {"critical": 2, "high": 5},
            "sources": [
                {"key": "ga4", "label": "Analytics", "available": True, "detail": "ok"},
                {"key": "gsc", "label": "Search Console", "available": False, "detail": "no"},
            ],
            "sections": [
                {"key": "sec", "number": "01", "title": "Security", "headline": "h",
                 "metrics": [], "findings": [], "tables": [], "notes": [], "unavailable": None},
            ],
        }
        data.update(over)
        return render_report(data)

    def test_the_cover_leads_with_the_period(self) -> None:
        assert '<p class="period">1 – 31 Mar 2026</p>' in self._html()

    def test_the_cover_survives_backgrounds_not_printing(self) -> None:
        # The old cover was white text on a gradient. Browsers do not print
        # background graphics by default, so the first page came out blank.
        # Print now gets dark ink on white, with the brand carried by rules.
        html = self._html()
        assert ".cover{background:#fff;color:#2E2E2E" in html
        assert "border-bottom:3px solid #0129AC" in html

    def test_the_cover_fills_the_page(self) -> None:
        # Rather than a band of text at the top of an empty sheet.
        assert "min-height:100vh;display:flex;flex-direction:column" in self._html()

    def test_findings_are_summarised_with_labels_not_colour_alone(self) -> None:
        html = self._html()
        assert "<b>2</b> critical" in html and "<b>5</b> high" in html

    def test_a_clean_report_says_so_rather_than_showing_nothing(self) -> None:
        assert "No findings raised" in self._html(severity_counts={})

    def test_source_availability_is_on_the_cover(self) -> None:
        assert "1 of 2" in self._html()

    def test_the_printed_document_has_contents(self) -> None:
        # The screen has a sticky sidebar, which print hides — leaving the
        # printed document with no way in at all.
        html = self._html()
        assert "What is inside" in html
        assert "<span>01</span>Security" in html

    def test_the_scope_note_is_printed(self) -> None:
        assert "current state" in self._html()
