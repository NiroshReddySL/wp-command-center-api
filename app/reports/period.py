"""The window a report covers.

Every period-scoped figure in a report resolves its dates from here, so a
report titled "1–31 March" cannot contain a section that quietly measured the
last 28 days instead. That was the risk in taking a range as a display string
while each section computed its own `now() - 28 days`: the heading and the
numbers would disagree, and nothing would look broken.

Not every section is period-scoped, and pretending otherwise would be the
same lie in the other direction. Which plugins are outdated, how many alerts
are open, how many pages score badly — these are facts about the site *now*,
and no stored history could reconstruct what they were in March. Those
sections say "as at <generated>"; `Period.scope_note` is the sentence the
report uses to state the distinction rather than leave a reader to assume.
"""
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

from app.utils.date_ranges import resolve_date_range

# What a report covers when the caller does not say. Four weeks: long enough
# for a weekly cadence to show a trend, short enough that a change is still
# recent enough to act on.
DEFAULT_RANGE = "28d"

_MONTH_FIRST = "%d %b"
_FULL = "%d %b %Y"


@dataclass(frozen=True)
class Period:
    start: date
    end: date

    @classmethod
    def from_request(
        cls, range_key: str, start_date: str | None, end_date: str | None
    ) -> "Period":
        """Resolve the same preset picker every other range control uses.

        Shared deliberately: a report for "last 28 days" and a dashboard for
        "last 28 days" have to mean the same 28 days, or comparing them is
        meaningless.
        """
        start, end = resolve_date_range(range_key or DEFAULT_RANGE, start_date, end_date)
        return cls(date.fromisoformat(start), date.fromisoformat(end))

    @classmethod
    def last_days(cls, days: int) -> "Period":
        today = datetime.now(UTC).date()
        return cls(today - timedelta(days=max(1, days) - 1), today)

    @property
    def days(self) -> int:
        """Inclusive length — 1 March to 31 March is 31 days, not 30."""
        return (self.end - self.start).days + 1

    @property
    def start_iso(self) -> str:
        return self.start.isoformat()

    @property
    def end_iso(self) -> str:
        return self.end.isoformat()

    @property
    def start_dt(self) -> datetime:
        """Midnight at the start of the first day, UTC — for `>=` comparisons
        against timestamp columns."""
        return datetime.combine(self.start, datetime.min.time(), tzinfo=UTC)

    @property
    def end_dt(self) -> datetime:
        """The instant after the last day ends, so `< end_dt` includes every
        moment of the final day rather than only its first instant."""
        return datetime.combine(self.end + timedelta(days=1), datetime.min.time(), tzinfo=UTC)

    @property
    def label(self) -> str:
        """"13 Jul – 10 Aug 2026", or "1 – 31 Mar 2026" within one month."""
        if self.start == self.end:
            return self.start.strftime(_FULL)
        if self.start.year != self.end.year:
            return f"{self.start.strftime(_FULL)} – {self.end.strftime(_FULL)}"
        if self.start.month == self.end.month:
            return f"{self.start.day} – {self.end.strftime(_FULL)}"
        return f"{self.start.strftime(_MONTH_FIRST)} – {self.end.strftime(_FULL)}"

    @property
    def is_historical(self) -> bool:
        """True when the window closed before today.

        Matters for what a report may claim: a closed period is final, while
        one still running will read differently tomorrow.
        """
        return self.end < datetime.now(UTC).date()

    def scope_note(self, generated: datetime) -> str:
        """How to read the two kinds of section in this report."""
        return (
            f"Traffic, search and conversion figures cover {self.label} "
            f"({self.days} days). Security, site health and content figures describe "
            f"the site as it stands on {generated.strftime(_FULL)} — they are its "
            "current state, not a reconstruction of what it was during the period."
        )

    def shortfall_note(self, recorded_days: int) -> str | None:
        """Said when the window has more days than the data does, because a
        total over 9 recorded days of a 28-day window is not a period total."""
        if recorded_days >= self.days:
            return None
        return (
            f"Only {recorded_days} of the {self.days} days in this period have "
            "recorded data, so the totals understate it."
        )


__all__ = ["DEFAULT_RANGE", "Period"]
