from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.engine import get_db
from app.database.models import Alert, ContentPost, ReviewItem, Site

router = APIRouter()


class DashboardMetrics(BaseModel):
    total_issues: int
    total_issues_change: float | None
    avg_health_score: float
    health_trend: list[int]
    content_published_week: int
    content_published_change: float | None
    uptime_percent: float | None


class PriorityItem(BaseModel):
    id: str
    severity: str
    agent: str
    title: str
    site_name: str
    site_id: str
    created_at: datetime
    action_type: str


class ActivityItem(BaseModel):
    id: str
    agent: str
    description: str
    site_name: str
    created_at: datetime
    link: str | None = None


class AgentSummary(BaseModel):
    agent: str
    open_count: int
    critical_count: int
    last_activity_at: datetime | None


@router.get("/metrics", response_model=DashboardMetrics)
async def get_metrics(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    now = datetime.now(UTC)
    week_ago = now - timedelta(days=7)
    two_weeks_ago = now - timedelta(days=14)

    # Open issues count
    issues_now_r = await db.execute(
        select(func.count(Alert.id)).where(Alert.status == "open")
    )
    total_issues = issues_now_r.scalar_one() or 0

    # Issues change vs prior week
    issues_last_r = await db.execute(
        select(func.count(Alert.id)).where(
            Alert.status == "open",
            Alert.created_at >= two_weeks_ago,
            Alert.created_at < week_ago,
        )
    )
    issues_last = issues_last_r.scalar_one() or 0
    issues_this_r = await db.execute(
        select(func.count(Alert.id)).where(
            Alert.status == "open",
            Alert.created_at >= week_ago,
        )
    )
    issues_this = issues_this_r.scalar_one() or 0
    if issues_last > 0:
        issues_change: float | None = round(((issues_this - issues_last) / issues_last) * 100, 1)
    else:
        issues_change = None

    # Average health score across all sites — aggregate in SQL
    avg_r = await db.execute(select(func.avg(Site.health_score)))
    avg_health = round(avg_r.scalar_one() or 0.0, 1)

    # Content published this week vs last
    pub_this_r = await db.execute(
        select(func.count(ContentPost.id)).where(ContentPost.created_at >= week_ago)
    )
    pub_this = pub_this_r.scalar_one() or 0

    pub_last_r = await db.execute(
        select(func.count(ContentPost.id)).where(
            ContentPost.created_at >= two_weeks_ago,
            ContentPost.created_at < week_ago,
        )
    )
    pub_last = pub_last_r.scalar_one() or 0
    if pub_last > 0:
        pub_change: float | None = round(((pub_this - pub_last) / pub_last) * 100, 1)
    else:
        pub_change = None

    # Uptime: % of performance snapshots from past 7 days with speed_score > 0
    from app.database.models import PerformanceSnapshot
    snap_total_r = await db.execute(
        select(func.count(PerformanceSnapshot.id)).where(
            PerformanceSnapshot.snapshot_at >= week_ago
        )
    )
    snap_total = snap_total_r.scalar_one() or 0
    snap_ok_r = await db.execute(
        select(func.count(PerformanceSnapshot.id)).where(
            PerformanceSnapshot.snapshot_at >= week_ago,
            PerformanceSnapshot.speed_score > 0,
        )
    )
    snap_ok = snap_ok_r.scalar_one() or 0
    uptime: float | None = round((snap_ok / snap_total) * 100, 2) if snap_total > 0 else None

    return {
        "total_issues": total_issues,
        "total_issues_change": issues_change,
        "avg_health_score": avg_health,
        "health_trend": [int(avg_health)] * 10,
        "content_published_week": pub_this,
        "content_published_change": pub_change,
        "uptime_percent": uptime,
    }


@router.get("/priority-queue", response_model=list[PriorityItem])
async def get_priority_queue(db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    result = await db.execute(
        select(Alert, Site.name.label("site_name"))
        .join(Site, Alert.site_id == Site.id)
        .where(Alert.status == "open")
        .order_by(Alert.created_at.desc())
        .limit(8)
    )
    rows = result.all()

    severity_order = {"critical": 0, "warning": 1, "info": 2}
    items = [
        {
            "id": alert.id,
            "severity": alert.severity,
            "agent": alert.agent,
            "title": alert.title,
            "site_name": site_name,
            "site_id": alert.site_id,
            "created_at": alert.created_at,
            "action_type": "fix" if alert.severity == "critical" else "review",
        }
        for alert, site_name in rows
    ]
    items.sort(key=lambda x: severity_order.get(x["severity"], 99))
    return items


@router.get("/agents", response_model=list[AgentSummary])
async def get_agent_summary(
    site_id: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    """Per-agent rollup for the dashboard command strip."""
    out: list[dict[str, Any]] = []

    # Watchdog + Optimizer surface open alerts
    for agent in ("watchdog", "optimizer"):
        base = select(Alert).where(Alert.agent == agent, Alert.status == "open")
        if site_id:
            base = base.where(Alert.site_id == site_id)

        open_count = (await db.execute(
            select(func.count()).select_from(base.subquery())
        )).scalar_one() or 0

        crit_q = base.where(Alert.severity == "critical")
        critical_count = (await db.execute(
            select(func.count()).select_from(crit_q.subquery())
        )).scalar_one() or 0

        last_q = select(func.max(Alert.created_at)).where(
            Alert.agent == agent, Alert.status == "open"
        )
        if site_id:
            last_q = last_q.where(Alert.site_id == site_id)
        last_activity = (await db.execute(last_q)).scalar_one_or_none()

        out.append({
            "agent": agent,
            "open_count": open_count,
            "critical_count": critical_count,
            "last_activity_at": last_activity,
        })

    # Autopilot surfaces pending review items (drafts awaiting approval)
    rq = select(ReviewItem).where(
        ReviewItem.agent == "autopilot", ReviewItem.status == "pending"
    )
    if site_id:
        rq = rq.where(ReviewItem.site_id == site_id)
    pending = (await db.execute(
        select(func.count()).select_from(rq.subquery())
    )).scalar_one() or 0

    last_rq = select(func.max(ReviewItem.created_at)).where(
        ReviewItem.agent == "autopilot", ReviewItem.status == "pending"
    )
    if site_id:
        last_rq = last_rq.where(ReviewItem.site_id == site_id)
    last_review = (await db.execute(last_rq)).scalar_one_or_none()

    out.append({
        "agent": "autopilot",
        "open_count": pending,
        "critical_count": 0,
        "last_activity_at": last_review,
    })

    return out


@router.get("/traffic-overview")
async def get_traffic_overview(db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    """
    If Google Analytics is connected, returns real daily page views per site
    as { date, <site_name>: views } for the last 30 days.
    Otherwise returns per-site totals from content_posts.
    """
    from app.api.auth import get_google_token
    from app.connectors.analytics import AnalyticsConnector
    from app.database.models import SiteConfig

    token = await get_google_token(db)
    sites_result = await db.execute(select(Site))
    sites = sites_result.scalars().all()

    if not sites:
        return []

    # Try GA4 if connected
    if token:
        ga = AnalyticsConnector(token.access_token)
        # Aggregate daily views across all sites with GA configured
        daily: dict[str, dict[str, Any]] = {}
        any_ga_data = False

        # Load all site configs in one query instead of one per site
        cfgs_r = await db.execute(select(SiteConfig))
        cfg_by_site = {c.site_id: c for c in cfgs_r.scalars().all()}

        for site in sites:
            cfg = cfg_by_site.get(site.id)
            if not cfg or not cfg.ga_property_id:
                continue

            try:
                rows = await ga.get_daily_page_views(cfg.ga_property_id, days=30)
                any_ga_data = True
                for row in rows:
                    sort_key = row["sort_key"]  # "20260409" — safe for chronological sort
                    if sort_key not in daily:
                        daily[sort_key] = {"date": row["date"], "_sort": sort_key}
                    daily[sort_key][site.name] = row["views"]
            except Exception:
                continue

        if any_ga_data:
            sorted_rows = sorted(daily.values(), key=lambda x: x["_sort"])
            # Strip the internal sort key before returning
            return [{k: v for k, v in row.items() if k != "_sort"} for row in sorted_rows]

    # Fallback: per-site traffic_30d totals — one grouped query for all sites
    totals_r = await db.execute(
        select(
            ContentPost.site_id,
            func.sum(ContentPost.traffic_30d),
            func.count(ContentPost.id),
        ).group_by(ContentPost.site_id)
    )
    totals_by_site = {sid: (traffic or 0, count or 0) for sid, traffic, count in totals_r.all()}

    return [
        {
            "site_id": site.id,
            "site_name": site.name,
            "traffic_30d": totals_by_site.get(site.id, (0, 0))[0],
            "post_count": totals_by_site.get(site.id, (0, 0))[1],
            "health_score": site.health_score,
        }
        for site in sites
    ]


@router.get("/activity", response_model=list[ActivityItem])
async def get_activity(db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    result = await db.execute(
        select(Alert, Site.name.label("site_name"))
        .join(Site, Alert.site_id == Site.id)
        .order_by(Alert.created_at.desc())
        .limit(10)
    )
    rows = result.all()

    verbs = {
        "watchdog": "Watchdog detected",
        "optimizer": "Optimizer found",
        "autopilot": "Autopilot generated",
    }
    return [
        {
            "id": alert.id,
            "agent": alert.agent,
            "description": f"{verbs.get(alert.agent, 'Agent flagged')}: {alert.title}",
            "site_name": site_name,
            "created_at": alert.created_at,
            "link": None,
        }
        for alert, site_name in rows
    ]
