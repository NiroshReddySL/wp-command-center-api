"""Traffic agent API — snapshots, trends, alerts, flush/re-run."""
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, Query
from pydantic import BaseModel
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.engine import AsyncSessionLocal, get_db
from app.database.models import Alert, Site, TrafficSnapshot
from app.security.rate_limit import ai_limiter, job_limiter
from app.utils.date_ranges import resolve_date_range

router = APIRouter()


# ── Schemas ───────────────────────────────────────────────────────────────────

class TrafficSnapshotResponse(BaseModel):
    id: str
    site_id: str
    site_name: str
    date: str
    pageviews: int
    sessions: int
    users: int
    bounce_rate: float
    avg_session_duration: float
    top_pages: list[dict[str, Any]]
    source: str
    snapshot_at: datetime

    model_config = {"from_attributes": True}


class TrafficSummary(BaseModel):
    site_id: str
    site_name: str
    snapshot_date: str
    is_stale: bool
    has_comparison: bool
    pageviews_today: int
    pageviews_yesterday: int
    change_pct: float
    sessions_today: int
    users_today: int
    bounce_rate: float
    avg_session_duration: float
    top_pages: list[dict[str, Any]]
    source: str


class TrafficAlertResponse(BaseModel):
    id: str
    site_id: str
    site_name: str
    severity: str
    type: str
    title: str
    description: str
    metadata: dict[str, Any]
    status: str
    created_at: datetime

    model_config = {"from_attributes": True}


class FlushRequest(BaseModel):
    site_id: str | None = None


class DailyForecast(BaseModel):
    date: str
    base: int
    optimistic: int
    pessimistic: int


class ForecastAnomaly(BaseModel):
    date: str
    type: str
    description: str
    severity: str


class TrafficPredictionResponse(BaseModel):
    site_id: str
    site_name: str
    horizon_days: int
    generated_at: datetime
    daily_forecasts: list[DailyForecast]
    anomalies: list[ForecastAnomaly]
    narrative: str
    model_version: str
    insufficient_data: bool = False
    # True when there WAS enough history, but the AI call itself failed
    # (bad/missing API key, rate limit, malformed response) and no earlier
    # prediction exists to fall back to — a real, transient error, distinct
    # from "insufficient_data" (which just needs more days of history).
    generation_failed: bool = False


class RegenerateRequest(BaseModel):
    site_id: str | None = None
    horizon_days: int = 7


# ── Pure helpers (unit-testable without a DB session) ─────────────────────────

def _pick_anchor_date(available_dates: set[str], today: str, yesterday: str) -> str | None:
    """Pick the most recent date we actually have a snapshot for, so a summary
    is never silently built from `None`. Prefers today; falls back to
    yesterday when today's collection hasn't run yet."""
    if today in available_dates:
        return today
    if yesterday in available_dates:
        return yesterday
    return None


def _is_stale(anchor_date: str, today: str, yesterday: str) -> bool:
    """A site is only flagged stale when even YESTERDAY's sync is missing.

    GA4 itself always finalizes data one day behind — every GA4 snapshot
    TrafficAgent writes is dated "yesterday", never "today" (see
    TrafficAgent._fetch_ga4), because GA4 needs a full day to close out
    metrics. Treating "anchor isn't today" as the staleness bar made every
    properly-working GA4-connected site permanently "stale" by definition,
    not by failure. Only a gap of 2+ days (anchor resolves to day_before,
    i.e. not even yesterday landed) is a genuine sync problem.
    """
    return anchor_date not in (today, yesterday)


def _change_pct(anchor_value: float, previous_value: float | None) -> tuple[float, bool]:
    """Day-over-day % change plus whether a real comparison point existed —
    lets the caller distinguish "flat" from "nothing to compare against"."""
    if not previous_value:
        return 0.0, False
    return round((anchor_value - previous_value) / previous_value * 100, 1), True


# ── Endpoints ─────────────────────────────────────────────────────────────────

@router.get("/snapshots", response_model=list[TrafficSnapshotResponse])
async def list_snapshots(
    site_id: str | None = None,
    source: str | None = Query(None, pattern="^(ga4|estimated)$"),
    range: str = "28d",
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = Query(300, ge=1, le=1000),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    since, until = resolve_date_range(range, start_date, end_date)
    # Order desc + limit keeps the MOST RECENT rows when capping, then restore
    # ascending order for charting.
    query = (
        select(TrafficSnapshot, Site.name.label("site_name"))
        .join(Site, TrafficSnapshot.site_id == Site.id)
        .where(TrafficSnapshot.date >= since, TrafficSnapshot.date <= until)
        .order_by(TrafficSnapshot.date.desc())
        .limit(limit)
    )
    if site_id:
        query = query.where(TrafficSnapshot.site_id == site_id)
    if source:
        query = query.where(TrafficSnapshot.source == source)

    rows = list(reversed((await db.execute(query)).all()))
    return [
        {
            "id": s.id, "site_id": s.site_id, "site_name": site_name,
            "date": s.date, "pageviews": s.pageviews, "sessions": s.sessions,
            "users": s.users, "bounce_rate": s.bounce_rate,
            "avg_session_duration": s.avg_session_duration,
            "top_pages": s.top_pages, "source": s.source, "snapshot_at": s.snapshot_at,
        }
        for s, site_name in rows
    ]


@router.get("/summary", response_model=list[TrafficSummary])
async def traffic_summary(
    site_id: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    """Per-site summary: today vs yesterday, top pages."""
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
    day_before = (datetime.now(timezone.utc) - timedelta(days=2)).strftime("%Y-%m-%d")

    sites_q = select(Site).where(Site.status != "inactive")
    if site_id:
        sites_q = sites_q.where(Site.id == site_id)
    sites = (await db.execute(sites_q)).scalars().all()

    result = []
    for site in sites:
        snaps_r = await db.execute(
            select(TrafficSnapshot)
            .where(TrafficSnapshot.site_id == site.id, TrafficSnapshot.date.in_([today, yesterday, day_before]))
        )
        snaps = {s.date: s for s in snaps_r.scalars().all()}

        anchor_date = _pick_anchor_date(set(snaps.keys()), today, yesterday)
        if anchor_date is None:
            continue
        t = snaps[anchor_date]
        prev_date = yesterday if anchor_date == today else day_before
        p = snaps.get(prev_date)

        change, has_comparison = _change_pct(t.pageviews, p.pageviews if p else None)

        result.append({
            "site_id": site.id, "site_name": site.name,
            "snapshot_date": anchor_date, "is_stale": _is_stale(anchor_date, today, yesterday),
            "has_comparison": has_comparison,
            "pageviews_today": t.pageviews, "pageviews_yesterday": p.pageviews if p else 0,
            "change_pct": change,
            "sessions_today": t.sessions, "users_today": t.users,
            "bounce_rate": t.bounce_rate, "avg_session_duration": t.avg_session_duration,
            "top_pages": t.top_pages, "source": t.source,
        })

    return result


_TREND_METRICS = {
    "pageviews": lambda s: s.pageviews,
    "sessions": lambda s: s.sessions,
    "users": lambda s: s.users,
    "bounce_rate": lambda s: s.bounce_rate,
    "avg_session_duration": lambda s: s.avg_session_duration,
}


@router.get("/trend")
async def traffic_trend(
    site_id: str | None = None,
    range: str = "28d",
    start_date: str | None = None,
    end_date: str | None = None,
    metric: str = Query("pageviews", pattern="^(pageviews|sessions|users|bounce_rate|avg_session_duration)$"),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    """Chart-ready daily series: [{date, <site_name>: <metric value>, ...}]"""
    since, until = resolve_date_range(range, start_date, end_date)
    query = (
        select(TrafficSnapshot, Site.name.label("site_name"))
        .join(Site, TrafficSnapshot.site_id == Site.id)
        .where(TrafficSnapshot.date >= since, TrafficSnapshot.date <= until)
        .order_by(TrafficSnapshot.date.asc())
    )
    if site_id:
        query = query.where(TrafficSnapshot.site_id == site_id)

    rows = (await db.execute(query)).all()
    extract = _TREND_METRICS[metric]
    daily: dict[str, dict[str, Any]] = {}
    for snap, sname in rows:
        if snap.date not in daily:
            daily[snap.date] = {"date": snap.date}
        daily[snap.date][sname] = extract(snap)

    return list(daily.values())


@router.get("/alerts", response_model=list[TrafficAlertResponse])
async def list_traffic_alerts(
    site_id: str | None = None,
    status: str | None = None,
    severity: list[str] = Query(default=[]),
    alert_type: list[str] = Query(default=[]),
    limit: int = Query(200, ge=1, le=500),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    query = (
        select(Alert, Site.name.label("site_name"))
        .join(Site, Alert.site_id == Site.id)
        .where(Alert.agent == "traffic")
        .order_by(Alert.created_at.desc())
        .limit(limit)
    )
    if site_id:
        query = query.where(Alert.site_id == site_id)
    if status:
        query = query.where(Alert.status == status)
    else:
        query = query.where(Alert.status.in_(["open", "acknowledged"]))
    if severity:
        query = query.where(Alert.severity.in_(severity))
    if alert_type:
        query = query.where(Alert.type.in_(alert_type))

    rows = (await db.execute(query)).all()
    return [
        {
            "id": a.id, "site_id": a.site_id, "site_name": sname,
            "severity": a.severity, "type": a.type,
            "title": a.title, "description": a.description,
            "metadata": a.metadata_, "status": a.status, "created_at": a.created_at,
        }
        for a, sname in rows
    ]


def _aggregate_top_pages(
    snapshots: list[tuple[str, str, list[dict[str, Any]]]],
) -> list[dict[str, Any]]:
    """Sum page views across snapshots, keyed by (site_id, path) — two sites
    sharing the same relative path (both have "/about", say) must never be
    silently merged into one misleading row."""
    pages: dict[tuple[str, str], dict[str, Any]] = {}
    for site_id, site_name, top_pages in snapshots:
        for p in top_pages:
            path = p.get("path") or p.get("url", "")
            key = (site_id, path)
            if key not in pages:
                pages[key] = {**p, "site_id": site_id, "site_name": site_name, "views": 0}
            pages[key]["views"] += p.get("views", 0)
    return sorted(pages.values(), key=lambda x: x["views"], reverse=True)


@router.get("/top-pages")
async def top_pages(
    site_id: str | None = None,
    range: str = "28d",
    start_date: str | None = None,
    end_date: str | None = None,
    limit: int = Query(10, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    """Aggregate top pages across the selected range, attributed per site."""
    since, until = resolve_date_range(range, start_date, end_date)
    query = (
        select(TrafficSnapshot, Site.name.label("site_name"))
        .join(Site, TrafficSnapshot.site_id == Site.id)
        .where(TrafficSnapshot.date >= since, TrafficSnapshot.date <= until)
    )
    if site_id:
        query = query.where(TrafficSnapshot.site_id == site_id)

    rows = (await db.execute(query)).all()
    aggregated = _aggregate_top_pages([(snap.site_id, sname, snap.top_pages) for snap, sname in rows])
    return aggregated[:limit]


def _aggregate_geo(snapshots: list[dict[str, list[dict[str, Any]]]]) -> dict[str, Any]:
    """Sum country/region/city views across a set of snapshots (each a
    {"countries": [...], "regions": [...], "cities": [...]} dict)."""
    country_totals: dict[str, dict[str, Any]] = {}
    region_totals: dict[str, int] = {}
    city_totals: list[dict[str, Any]] = []

    for snap in snapshots:
        for c in (snap.get("countries") or []):
            key = c.get("country_code", c.get("country", ""))
            if key not in country_totals:
                country_totals[key] = {**c, "views": 0, "sessions": 0}
            country_totals[key]["views"] += c.get("views", 0)
            country_totals[key]["sessions"] += c.get("sessions", 0)

        for r in (snap.get("regions") or []):
            region_totals[r["region"]] = region_totals.get(r["region"], 0) + r.get("views", 0)

        city_totals.extend(snap.get("cities") or [])

    total_views = sum(c["views"] for c in country_totals.values()) or 1
    countries = sorted(
        [{**c, "pct": round(c["views"] / total_views * 100, 1)} for c in country_totals.values()],
        key=lambda x: x["views"], reverse=True,
    )

    total_r = sum(region_totals.values()) or 1
    regions = sorted(
        [{"region": k, "views": v, "pct": round(v / total_r * 100, 1)} for k, v in region_totals.items()],
        key=lambda x: x["views"], reverse=True,
    )

    # Merge city duplicates
    city_map: dict[str, dict[str, Any]] = {}
    for city in city_totals:
        key = f"{city.get('city')}|{city.get('country')}"
        if key not in city_map:
            city_map[key] = {**city, "views": 0}
        city_map[key]["views"] += city.get("views", 0)
    cities = sorted(city_map.values(), key=lambda x: x["views"], reverse=True)[:25]

    return {"countries": countries, "regions": regions, "cities": cities}


@router.get("/geo")
async def geo_breakdown(
    site_id: str | None = None,
    range: str = "28d",
    start_date: str | None = None,
    end_date: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Aggregate geo breakdown across the selected range."""
    since, until = resolve_date_range(range, start_date, end_date)
    query = select(TrafficSnapshot).where(TrafficSnapshot.date >= since, TrafficSnapshot.date <= until)
    if site_id:
        query = query.where(TrafficSnapshot.site_id == site_id)

    snaps = (await db.execute(query)).scalars().all()
    return _aggregate_geo([
        {"countries": s.geo_countries, "regions": s.geo_regions, "cities": s.geo_cities}
        for s in snaps
    ])


# ── AI Predictions ───────────────────────────────────────────────────────────

@router.get("/predictions", response_model=list[TrafficPredictionResponse])
async def get_predictions(
    site_id: str | None = None,
    horizon_days: int = 7,
    force: bool = False,
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    """Return cached AI traffic predictions (regenerates if stale or force=true)."""
    from app.services.traffic_prediction import PredictionGenerationFailed, TrafficPredictionService

    sites_q = select(Site).where(Site.status != "inactive")
    if site_id:
        sites_q = sites_q.where(Site.id == site_id)
    sites = (await db.execute(sites_q)).scalars().all()

    svc = TrafficPredictionService(db)
    results = []
    for site in sites:
        try:
            pred = await svc.get_or_generate(site.id, horizon_days, force)
        except PredictionGenerationFailed:
            results.append({
                "site_id": site.id, "site_name": site.name,
                "horizon_days": horizon_days,
                "generated_at": datetime.now(timezone.utc),
                "daily_forecasts": [], "anomalies": [],
                "narrative": "", "model_version": "gpt-4o",
                "insufficient_data": False, "generation_failed": True,
            })
            continue
        if pred is None:
            results.append({
                "site_id": site.id, "site_name": site.name,
                "horizon_days": horizon_days,
                "generated_at": datetime.now(timezone.utc),
                "daily_forecasts": [], "anomalies": [],
                "narrative": "", "model_version": "gpt-4o",
                "insufficient_data": True,
            })
        else:
            results.append({
                "site_id": site.id, "site_name": site.name,
                "horizon_days": pred.horizon_days,
                "generated_at": pred.generated_at,
                "daily_forecasts": pred.daily_forecasts,
                "anomalies": pred.anomalies,
                "narrative": pred.narrative,
                "model_version": pred.model_version,
                "insufficient_data": False,
            })
    await db.commit()
    return results


async def _background_predict(site_id: str | None, horizon_days: int) -> None:
    from app.services.traffic_prediction import TrafficPredictionService

    async with AsyncSessionLocal() as db:
        sites_q = select(Site).where(Site.status != "inactive")
        if site_id:
            sites_q = sites_q.where(Site.id == site_id)
        sites = (await db.execute(sites_q)).scalars().all()

        svc = TrafficPredictionService(db)
        for site in sites:
            for h in ([horizon_days] if horizon_days else [7, 14, 30]):
                try:
                    await svc.get_or_generate(site.id, h, force=True)
                except Exception as exc:
                    import logging
                    logging.getLogger(__name__).error("Prediction failed for %s h=%d: %s", site.id, h, exc)
        await db.commit()


@router.post("/predictions/regenerate", dependencies=[Depends(ai_limiter)])
async def regenerate_predictions(
    req: RegenerateRequest,
    background_tasks: BackgroundTasks,
) -> dict[str, str]:
    """Trigger background regeneration of predictions for given site + horizon."""
    background_tasks.add_task(_background_predict, req.site_id, req.horizon_days)
    return {"status": "regenerating", "message": "Predictions are being regenerated in the background"}


# ── Flush & Re-run ────────────────────────────────────────────────────────────

async def _run_traffic_agent(site_id: str | None) -> None:
    import logging as _logging
    _logger = _logging.getLogger(__name__)
    from app.agents.traffic.traffic_agent import TrafficAgent

    async with AsyncSessionLocal() as db:
        sites_q = select(Site.id).where(Site.status != "inactive")
        if site_id:
            sites_q = sites_q.where(Site.id == site_id)
        site_ids = [row[0] for row in (await db.execute(sites_q)).all()]

    # Fresh session + commit per site — one failing site doesn't lose the rest
    for sid in site_ids:
        async with AsyncSessionLocal() as db:
            try:
                await TrafficAgent(db).run(sid)
                await db.commit()
            except Exception as exc:
                await db.rollback()
                _logger.warning("Traffic agent failed for site %s: %s", sid, exc)


@router.post("/flush", dependencies=[Depends(job_limiter)])
async def flush_traffic(
    req: FlushRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> dict[str, str]:
    # Only clear open alerts — snapshots are historical data that predictions depend on
    aq = delete(Alert).where(Alert.agent == "traffic")
    if req.site_id:
        aq = aq.where(Alert.site_id == req.site_id)
    await db.execute(aq)
    await db.commit()

    background_tasks.add_task(_run_traffic_agent, req.site_id)
    return {"status": "flushed", "message": "Traffic agent re-running in background"}
