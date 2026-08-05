"""What can actually be evidenced right now.

Probed before any section is built, because the alternative — assuming a
source works and rendering whatever comes back — is how a report ends up
stating "0 vulnerabilities" about components nobody scanned, or "0 sessions"
about a site whose analytics authorisation lapsed.

Every probe answers three things: is it usable, how fresh is it, and how much
of the estate does it cover. Partial availability is the normal case here and
has to survive into the report rather than being rounded up to "fine".
"""
from datetime import UTC, datetime, timedelta

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.models import (
    ContentPost,
    OAuthToken,
    PerformanceSnapshot,
    PluginAudit,
    Site,
    TrafficSnapshot,
    VulnerabilityCache,
)
from app.reports.analysed import ANALYSED
from app.reports.models import SourceStatus

# Beyond this a snapshot describes a site that may since have changed.
_STALE_AFTER = timedelta(days=3)


def _ago(when: datetime | None) -> str:
    if when is None:
        return "never"
    days = (datetime.now(UTC) - when).days
    if days <= 0:
        return "today"
    return "yesterday" if days == 1 else f"{days} days ago"


async def probe_google(db: AsyncSession) -> list[SourceStatus]:
    """Google Analytics and Search Console.

    Connected is not the same as authorised: Google issues a valid token even
    when the sensitive scopes are declined, and every data call then fails
    with a permissions error. So the granted scopes are what decides this,
    never the mere existence of a token.
    """
    from app.api.auth import capabilities

    token = (await db.execute(
        select(OAuthToken).where(OAuthToken.provider == "google")
    )).scalar_one_or_none()

    if token is None:
        reason = "No Google account connected"
        return [
            SourceStatus("ga4", "Google Analytics 4", False, reason),
            SourceStatus("gsc", "Google Search Console", False, reason),
        ]

    caps = capabilities(token.scope)
    denied = (
        "Connected, but the Analytics permission was not granted — every "
        "request is refused with a permissions error"
    )
    denied_gsc = (
        "Connected, but the Search Console permission was not granted — every "
        "request is refused with a permissions error"
    )
    return [
        SourceStatus(
            "ga4", "Google Analytics 4", caps["analytics"],
            "Authorised" if caps["analytics"] else denied,
        ),
        SourceStatus(
            "gsc", "Google Search Console", caps["search_console"],
            "Authorised" if caps["search_console"] else denied_gsc,
        ),
    ]


async def probe_traffic(db: AsyncSession, site_id: str) -> SourceStatus:
    """Stored traffic snapshots — usable even when GA4 itself is unreachable,
    provided they are recent enough to describe the period being reported."""
    newest = (await db.execute(
        select(func.max(TrafficSnapshot.date)).where(TrafficSnapshot.site_id == site_id)
    )).scalar_one_or_none()
    if newest is None:
        return SourceStatus("traffic", "Traffic snapshots", False, "No snapshots recorded")

    # `date` is stored as an ISO string, not a DATE column.
    try:
        as_of = datetime.fromisoformat(newest).replace(tzinfo=UTC)
    except ValueError:
        return SourceStatus(
            "traffic", "Traffic snapshots", False, f"Unreadable snapshot date {newest!r}"
        )
    fresh = datetime.now(UTC) - as_of <= _STALE_AFTER
    return SourceStatus(
        "traffic", "Traffic snapshots", fresh,
        f"Most recent snapshot {newest} ({_ago(as_of)})"
        + ("" if fresh else " — too old to describe the current period"),
        as_of=as_of,
    )


async def probe_components(db: AsyncSession, site_id: str) -> SourceStatus:
    """Vulnerability data, and crucially how much of the inventory it covers.

    A component with no successful WPScan lookup is unknown, not clean. The
    report must be able to say so, so the covered fraction travels with the
    status rather than being silently folded into a total.
    """
    total = (await db.execute(
        select(func.count()).select_from(PluginAudit).where(PluginAudit.site_id == site_id)
    )).scalar_one()
    if not total:
        return SourceStatus("components", "Plugin & theme inventory", False, "No components tracked")

    slugs = (await db.execute(
        select(PluginAudit.component_type, PluginAudit.plugin_slug)
        .where(PluginAudit.site_id == site_id)
    )).all()
    keys = {(t, s) for t, s in slugs}

    rows = (await db.execute(
        select(VulnerabilityCache.component_type, VulnerabilityCache.slug,
               VulnerabilityCache.fetched_at)
        .where(VulnerabilityCache.fetched_at.isnot(None))
    )).all()
    checked = {(t, s) for t, s, _ in rows if (t, s) in keys}
    stamps = [f for t, s, f in rows if (t, s) in keys and f]

    if not settings.WPSCAN_API_KEY:
        return SourceStatus(
            "components", "WPScan vulnerability database", False,
            "No WPScan API key configured — components are checked for updates "
            "but not for known vulnerabilities",
            coverage=f"0 of {total} components",
        )

    oldest = min(stamps) if stamps else None
    return SourceStatus(
        "components", "WPScan vulnerability database", bool(checked),
        f"{len(checked)} of {total} components have a vulnerability result"
        + (f"; oldest checked {_ago(oldest)}" if oldest else ""),
        as_of=max(stamps) if stamps else None,
        coverage=f"{len(checked)} of {total} components",
    )


async def probe_pagespeed(db: AsyncSession, site_id: str) -> SourceStatus:
    """PageSpeed results, separating real Lighthouse runs from the TTFB
    fallback used when the API is unavailable. Averaging the two together
    would produce a number that means nothing."""
    since = datetime.now(UTC) - timedelta(days=30)
    rows = (await db.execute(
        select(PerformanceSnapshot)
        .where(PerformanceSnapshot.site_id == site_id, PerformanceSnapshot.snapshot_at >= since)
        .order_by(PerformanceSnapshot.snapshot_at.desc())
    )).scalars().all()
    if not rows:
        return SourceStatus("psi", "PageSpeed Insights", False, "No measurements in the last 30 days")

    # A TTFB-estimated row carries no Core Web Vitals, which is what
    # distinguishes it from a real Lighthouse run.
    real = [r for r in rows if r.lcp or r.cls]
    newest = rows[0].snapshot_at
    detail = f"{len(rows)} measurement(s) in 30 days, most recent {_ago(newest)}"
    if len(real) < len(rows):
        detail += (
            f"; {len(rows) - len(real)} are TTFB estimates rather than Lighthouse "
            "runs and are excluded from Core Web Vitals figures"
        )
    return SourceStatus(
        "psi", "PageSpeed Insights", bool(real), detail,
        as_of=newest, coverage=f"{len(real)} of {len(rows)} are full measurements",
    )


async def probe_content(db: AsyncSession, site_id: str) -> SourceStatus:
    """Content scoring coverage — a page that has never been scored cannot
    contribute to a portfolio figure, so the unscored share is stated."""
    total = (await db.execute(
        select(func.count()).select_from(ContentPost).where(ContentPost.site_id == site_id)
    )).scalar_one()
    if not total:
        return SourceStatus("content", "Content scoring", False, "No content synced")

    # A page carries a default health_score of 50 until it is actually
    # analysed, so a non-zero score proves nothing. An empty score_breakdown
    # is the honest test: no breakdown means no analysis happened, and
    # counting those as "scored" would inflate coverage and drag the median
    # onto the placeholder value.
    scored = (await db.execute(
        select(func.count()).select_from(ContentPost)
        .where(ContentPost.site_id == site_id, ANALYSED)
    )).scalar_one()
    newest = (await db.execute(
        select(func.max(ContentPost.updated_at)).where(ContentPost.site_id == site_id)
    )).scalar_one_or_none()
    return SourceStatus(
        "content", "Content scoring", scored > 0,
        f"{scored} of {total} pages scored, last updated {_ago(newest)}",
        as_of=newest, coverage=f"{scored} of {total} pages",
    )


async def probe_wordpress(db: AsyncSession, site: Site) -> SourceStatus:
    """Whether the site itself can be read. Without an Application Password
    the plugin and theme inventory is whatever someone entered by hand, which
    is a materially weaker claim and has to be labelled as one."""
    return SourceStatus(
        "wordpress", "WordPress REST API", bool(site.api_key),
        "Application Password connected — inventory read directly from the site"
        if site.api_key else
        "No Application Password — plugins and themes are only those recorded by hand",
    )


async def probe_all(db: AsyncSession, site: Site) -> list[SourceStatus]:
    google = await probe_google(db)
    return [
        await probe_wordpress(db, site),
        *google,
        await probe_traffic(db, site.id),
        await probe_content(db, site.id),
        await probe_components(db, site.id),
        await probe_pagespeed(db, site.id),
    ]


__all__ = ["probe_all", "probe_components", "probe_content", "probe_google",
           "probe_pagespeed", "probe_traffic", "probe_wordpress"]
