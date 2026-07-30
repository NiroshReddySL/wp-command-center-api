"""Shared GA4-style date range resolution — used by both Live Visitors and
Flow Categories' date pickers, so every preset (today/yesterday/7d/28d/90d/
qtd/ytd/custom) resolves identically wherever it's offered, and the
"compare to previous period" window is always computed the same way.
"""
from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi import HTTPException

from app.utils.date_ranges import previous_period, quarter_start, resolve_date_range


class TestResolveDateRange:
    """Every preset resolves to REAL calendar dates (not GA4's "today" /
    "NdaysAgo" relative keywords) — needed to label a range, name exports
    after actual dates, and enumerate exact days for a day-wise breakdown.
    Expectations are computed from the real clock so these don't rot into a
    flaky hardcoded-date test."""

    def test_today_preset(self) -> None:
        today = datetime.now(timezone.utc).date().isoformat()
        assert resolve_date_range("today", None, None) == (today, today)

    def test_yesterday_preset(self) -> None:
        yesterday = (datetime.now(timezone.utc).date() - timedelta(days=1)).isoformat()
        assert resolve_date_range("yesterday", None, None) == (yesterday, yesterday)

    def test_7d_preset_spans_exactly_7_calendar_days_inclusive_of_today(self) -> None:
        today = datetime.now(timezone.utc).date()
        start, end = resolve_date_range("7d", None, None)
        assert end == today.isoformat()
        assert start == (today - timedelta(days=6)).isoformat()
        assert (date.fromisoformat(end) - date.fromisoformat(start)).days == 6  # 6 nights = 7 days

    def test_28d_preset_spans_28_days(self) -> None:
        start, end = resolve_date_range("28d", None, None)
        assert (date.fromisoformat(end) - date.fromisoformat(start)).days == 27

    def test_90d_preset_spans_90_days(self) -> None:
        start, end = resolve_date_range("90d", None, None)
        assert (date.fromisoformat(end) - date.fromisoformat(start)).days == 89

    def test_qtd_starts_on_the_first_of_the_current_quarter(self) -> None:
        today = datetime.now(timezone.utc).date()
        start, end = resolve_date_range("qtd", None, None)
        assert end == today.isoformat()
        assert date.fromisoformat(start) == quarter_start(today)
        assert date.fromisoformat(start).month in (1, 4, 7, 10)
        assert date.fromisoformat(start).day == 1

    def test_ytd_starts_on_january_first(self) -> None:
        today = datetime.now(timezone.utc).date()
        start, end = resolve_date_range("ytd", None, None)
        assert end == today.isoformat()
        assert start == date(today.year, 1, 1).isoformat()

    def test_custom_range_with_valid_dates(self) -> None:
        assert resolve_date_range("custom", "2026-01-01", "2026-01-31") == ("2026-01-01", "2026-01-31")

    def test_custom_range_missing_dates_raises_422(self) -> None:
        with pytest.raises(HTTPException) as exc_info:
            resolve_date_range("custom", None, None)
        assert exc_info.value.status_code == 422

    def test_custom_range_malformed_date_raises_422(self) -> None:
        with pytest.raises(HTTPException) as exc_info:
            resolve_date_range("custom", "01/01/2026", "2026-01-31")
        assert exc_info.value.status_code == 422

    def test_unknown_range_key_raises_422(self) -> None:
        with pytest.raises(HTTPException) as exc_info:
            resolve_date_range("last-year", None, None)
        assert exc_info.value.status_code == 422


class TestQuarterStart:
    def test_q1(self) -> None:
        assert quarter_start(date(2026, 2, 15)) == date(2026, 1, 1)

    def test_q2(self) -> None:
        assert quarter_start(date(2026, 5, 20)) == date(2026, 4, 1)

    def test_q3(self) -> None:
        assert quarter_start(date(2026, 7, 21)) == date(2026, 7, 1)

    def test_q4(self) -> None:
        assert quarter_start(date(2026, 11, 1)) == date(2026, 10, 1)

    def test_first_day_of_quarter_returns_itself(self) -> None:
        assert quarter_start(date(2026, 10, 1)) == date(2026, 10, 1)


class TestPreviousPeriod:
    """The comparison window for "compare to previous period" — always the
    same length as the selected range, always ending the day before it
    starts, never GA4's calendar-relative shortcuts (e.g. "previous month"
    for a range that doesn't start on the 1st)."""

    def test_seven_day_range_compares_against_the_preceding_seven_days(self) -> None:
        assert previous_period("2026-07-24", "2026-07-30") == ("2026-07-17", "2026-07-23")

    def test_single_day_compares_against_the_single_preceding_day(self) -> None:
        assert previous_period("2026-07-30", "2026-07-30") == ("2026-07-29", "2026-07-29")

    def test_previous_period_is_the_same_length_as_the_current_one(self) -> None:
        start, end = "2026-06-01", "2026-06-14"  # 14 days
        prev_start, prev_end = previous_period(start, end)
        current_len = (date.fromisoformat(end) - date.fromisoformat(start)).days
        prev_len = (date.fromisoformat(prev_end) - date.fromisoformat(prev_start)).days
        assert prev_len == current_len

    def test_previous_period_never_overlaps_the_current_one(self) -> None:
        start, end = "2026-07-24", "2026-07-30"
        _, prev_end = previous_period(start, end)
        assert date.fromisoformat(prev_end) < date.fromisoformat(start)

    def test_spans_a_month_boundary_correctly(self) -> None:
        assert previous_period("2026-08-01", "2026-08-07") == ("2026-07-25", "2026-07-31")
