"""Everything a section builder is allowed to read.

Search and analytics figures come from live API calls, so they are fetched
once here rather than per section — and, more importantly, fetched behind a
try. A section whose fetch failed reports that it failed; it does not fall
back to a stored approximation and present it as the real thing, because a
number that silently changes meaning is worse than a missing one.
"""
import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import OAuthToken, Site, SiteConfig
from app.reports.models import SourceStatus
from app.reports.period import Period

logger = logging.getLogger(__name__)

SEARCH_DAYS = 28
QUERY_LIMIT = 100


@dataclass
class ReportContext:
    db: AsyncSession
    site: Site
    sources: list[SourceStatus]
    # The window every figure below was fetched for. Held here rather than
    # rederived per section, so a section cannot describe a different span
    # from the one the report is titled with.
    period: Period = field(default_factory=lambda: Period.last_days(SEARCH_DAYS))

    # Live pulls. Empty list means "fetched, nothing there"; None means the
    # fetch did not happen or failed — the two must stay distinguishable.
    search_daily: list[dict[str, Any]] | None = None
    search_queries: list[dict[str, Any]] | None = None
    search_error: str | None = None

    ga_top_pages: list[dict[str, Any]] | None = None
    ga_error: str | None = None

    notes: list[str] = field(default_factory=list)

    def source(self, key: str) -> SourceStatus | None:
        return next((s for s in self.sources if s.key == key), None)

    def available(self, key: str) -> bool:
        status = self.source(key)
        return bool(status and status.available)

    @property
    def search_totals(self) -> dict[str, float] | None:
        """Period totals. CTR and position are recomputed from the daily rows
        rather than averaged: a mean of daily CTRs weights a quiet Sunday the
        same as a busy Tuesday, and a mean of daily positions is not a
        position at all."""
        if not self.search_daily:
            return None
        clicks = sum(int(d.get("clicks") or 0) for d in self.search_daily)
        impressions = sum(int(d.get("impressions") or 0) for d in self.search_daily)
        weighted = sum(
            float(d.get("position") or 0) * int(d.get("impressions") or 0)
            for d in self.search_daily
        )
        return {
            "clicks": clicks,
            "impressions": impressions,
            "ctr": (clicks / impressions * 100) if impressions else 0.0,
            "position": (weighted / impressions) if impressions else 0.0,
            "days": len(self.search_daily),
        }


async def build_context(
    db: AsyncSession, site: Site, sources: list[SourceStatus],
    period: Period | None = None,
) -> ReportContext:
    period = period or Period.last_days(SEARCH_DAYS)
    ctx = ReportContext(db=db, site=site, sources=sources, period=period)

    token = (await db.execute(
        select(OAuthToken).where(OAuthToken.provider == "google")
    )).scalar_one_or_none()
    config = (await db.execute(
        select(SiteConfig).where(SiteConfig.site_id == site.id)
    )).scalar_one_or_none()

    if token is None:
        ctx.search_error = "No Google account connected"
        ctx.ga_error = ctx.search_error
        return ctx

    gsc_ready = ctx.available("gsc") and config and config.gsc_site_url
    ga_ready = ctx.available("ga4") and config and config.ga_property_id

    if not gsc_ready:
        ctx.search_error = (
            "Search Console is not authorised for this account"
            if not ctx.available("gsc")
            else "No Search Console property is configured for this site"
        )
    if not ga_ready:
        ctx.ga_error = (
            "Google Analytics is not authorised for this account"
            if not ctx.available("ga4")
            else "No Analytics property is configured for this site"
        )

    async def pull_search() -> None:
        from app.connectors.search_console import SearchConsoleConnector

        gsc = await SearchConsoleConnector.from_refresh_token(token.refresh_token)
        ctx.search_daily, ctx.search_queries = await asyncio.gather(
            gsc.get_daily_search_metrics(
                config.gsc_site_url,
                start_date=period.start_iso, end_date=period.end_iso,
            ),
            gsc.get_top_queries(
                config.gsc_site_url, limit=QUERY_LIMIT,
                start_date=period.start_iso, end_date=period.end_iso,
            ),
        )

    async def pull_ga() -> None:
        from app.connectors.analytics import AnalyticsConnector

        ga = await AnalyticsConnector.from_refresh_token(token.refresh_token)
        ctx.ga_top_pages = await ga.get_top_pages(
            config.ga_property_id, limit=15,
            start_date=period.start_iso, end_date=period.end_iso,
        )

    jobs = []
    if gsc_ready:
        jobs.append(("search", pull_search()))
    if ga_ready:
        jobs.append(("ga", pull_ga()))

    if jobs:
        results = await asyncio.gather(*(coro for _, coro in jobs), return_exceptions=True)
        for (name, _), result in zip(jobs, results, strict=True):
            if isinstance(result, Exception):
                logger.warning("Report %s fetch failed for %s: %s", name, site.id, result)
                message = f"The request failed: {type(result).__name__}"
                if name == "search":
                    ctx.search_error = message
                    ctx.search_daily = ctx.search_queries = None
                else:
                    ctx.ga_error = message
                    ctx.ga_top_pages = None

    return ctx


__all__ = ["QUERY_LIMIT", "SEARCH_DAYS", "ReportContext", "build_context"]
