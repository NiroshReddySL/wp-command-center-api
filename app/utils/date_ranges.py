"""GA4-style date range resolution — shared by every endpoint that offers
the same preset picker (today/yesterday/7d/28d/90d/qtd/ytd/custom) GA4's own
reports use, so every such picker resolves ranges identically. Originally
lived only in watched_urls.py; pulled out once Flow Categories needed the
same presets plus a "previous period" for comparison.
"""
import re
from datetime import date, datetime, timedelta, timezone

from fastapi import HTTPException

RANGE_KEYS = frozenset({"today", "yesterday", "7d", "28d", "90d", "qtd", "ytd", "custom"})
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def quarter_start(today: date) -> date:
    first_month_of_quarter = ((today.month - 1) // 3) * 3 + 1
    return date(today.year, first_month_of_quarter, 1)


def resolve_date_range(range_key: str, start_date: str | None, end_date: str | None) -> tuple[str, str]:
    """Returns (start, end) as real YYYY-MM-DD calendar dates — "today" means
    the actual current date, not the keyword GA4 would also accept."""
    if range_key == "custom":
        if not (start_date and end_date and _DATE_RE.match(start_date) and _DATE_RE.match(end_date)):
            raise HTTPException(
                status_code=422, detail="Custom range requires start_date and end_date as YYYY-MM-DD"
            )
        return start_date, end_date

    if range_key not in RANGE_KEYS:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown range '{range_key}' — expected one of {', '.join(sorted(RANGE_KEYS))}",
        )

    today = datetime.now(timezone.utc).date()
    if range_key == "today":
        start = today
    elif range_key == "yesterday":
        start = today - timedelta(days=1)
        today = start  # single-day range
    elif range_key == "7d":
        start = today - timedelta(days=6)   # 7 calendar days inclusive of today
    elif range_key == "28d":
        start = today - timedelta(days=27)
    elif range_key == "90d":
        start = today - timedelta(days=89)
    elif range_key == "qtd":
        start = quarter_start(today)
    else:  # ytd
        start = date(today.year, 1, 1)

    return start.isoformat(), today.isoformat()


def previous_period(start_iso: str, end_iso: str) -> tuple[str, str]:
    """The immediately preceding period of equal length, for "compare to
    previous period" — e.g. 2026-07-24..2026-07-30 (7 days) returns
    2026-07-17..2026-07-23. Always the same size as the selected range and
    never overlapping it, unlike GA4's calendar-relative shortcuts (e.g.
    "previous month" for a mid-month range)."""
    start = date.fromisoformat(start_iso)
    end = date.fromisoformat(end_iso)
    length = (end - start).days + 1
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=length - 1)
    return prev_start.isoformat(), prev_end.isoformat()
