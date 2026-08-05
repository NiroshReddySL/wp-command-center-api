"""Assemble a report, then freeze it.

Generation is deliberately a two-step: probe what can be evidenced, then build
only from what came back available. A section whose source is missing renders
its reason instead of its content, so a reader can always tell the difference
between "we looked and found nothing" and "we could not look".

The result is stored as a snapshot rather than recomputed on view. A figure
that changes after the report was sent is worse than no figure — someone acts
on the version they were given, and it has to still say what it said.
"""
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import ReviewItem, Site
from app.reports.models import Report
from app.reports.sections import BUILDERS
from app.reports.sources import probe_all

logger = logging.getLogger(__name__)

REPORT_ACTION_TYPE = "site_report"
REPORT_PERIOD_DAYS = 28


async def build_report(db: AsyncSession, site: Site) -> Report:
    """Every section the available data supports, plus the gaps as stated facts."""
    now = datetime.now(UTC)
    sources = await probe_all(db, site)

    report = Report(
        site_name=site.name,
        site_url=site.url,
        period_start=(now - timedelta(days=REPORT_PERIOD_DAYS)).date().isoformat(),
        period_end=now.date().isoformat(),
        generated_at=now,
        sources=sources,
    )

    for build in BUILDERS:
        try:
            report.sections.append(await build(db, site.id, sources))
        except Exception as exc:
            # One section failing must not cost the whole report — but it also
            # must not silently vanish, which would read as "nothing to report".
            logger.exception("Report section %s failed for %s", build.__name__, site.id)
            from app.reports.models import Section

            report.sections.append(Section(
                key=build.__name__.removeprefix("build_"),
                number="—",
                title=build.__name__.removeprefix("build_").replace("_", " ").title(),
                headline="",
                unavailable=f"This section could not be produced: {exc}",
            ))

    return report


async def store_report(db: AsyncSession, site: Site, report: Report) -> ReviewItem:
    """Persist the frozen snapshot."""
    item = ReviewItem(
        agent="autopilot",
        action_type=REPORT_ACTION_TYPE,
        site_id=site.id,
        status="pending",
        payload={
            "title": f"{site.name} — Site Report — {report.generated_at.strftime('%d %b %Y')}",
            "report": report.to_dict(),
        },
    )
    db.add(item)
    await db.flush()
    return item


async def generate_and_store(db: AsyncSession, site: Site) -> ReviewItem:
    return await store_report(db, site, await build_report(db, site))


__all__ = ["REPORT_ACTION_TYPE", "REPORT_PERIOD_DAYS", "build_report",
           "generate_and_store", "store_report"]
