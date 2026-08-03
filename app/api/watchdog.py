import asyncio
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.engine import AsyncSessionLocal, get_db
from app.database.models import Alert, Site
from app.security.rate_limit import job_limiter

router = APIRouter()

# Where the last re-run outcome is stored, so a failed background run is
# visible in the UI instead of surfacing as an empty "all healthy" list.
_RUN_STATUS_KEY = "watchdog.last_run"


class AlertResponse(BaseModel):
    id: str
    site_id: str
    site_name: str
    agent: str
    severity: str
    type: str
    title: str
    description: str
    metadata: dict[str, Any]
    status: str
    created_at: datetime
    resolved_at: datetime | None

    model_config = {"from_attributes": True}


@router.get("/alerts", response_model=list[AlertResponse])
async def list_alerts(
    site_id: str | None = None,
    severity: str | None = None,
    agent: str | None = None,
    status: str | None = None,
    type: str | None = None,
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    """Cross-agent alert listing — pass `agent` to scope to one agent (the
    Watchdog page always does); omit it for a site's full alert history."""
    query = (
        select(Alert, Site.name.label("site_name"))
        .join(Site, Alert.site_id == Site.id)
        .order_by(Alert.created_at.desc())
        .limit(limit)
        .offset(offset)
    )

    if site_id:
        query = query.where(Alert.site_id == site_id)
    if severity:
        query = query.where(Alert.severity == severity)
    if agent:
        query = query.where(Alert.agent == agent)
    if status:
        query = query.where(Alert.status == status)
    else:
        query = query.where(Alert.status.in_(["open", "acknowledged"]))
    if type:
        # autoescape: `_` and `%` are LIKE wildcards, and this value comes
        # straight from the client — `type=_` matched every alert.
        query = query.where(Alert.type.contains(type, autoescape=True))

    result = await db.execute(query)
    rows = result.all()

    return [
        {
            "id": a.id,
            "site_id": a.site_id,
            "site_name": site_name,
            "agent": a.agent,
            "severity": a.severity,
            "type": a.type,
            "title": a.title,
            "description": a.description,
            "metadata": a.metadata_,
            "status": a.status,
            "created_at": a.created_at,
            "resolved_at": a.resolved_at,
        }
        for a, site_name in rows
    ]


class AlertSummary(BaseModel):
    total: int
    by_type: dict[str, int]        # buckets: broken_link | performance | plugin | other
    by_severity: dict[str, int]
    matrix: dict[str, dict[str, int]]  # bucket -> severity -> count


def _bucket(alert_type: str) -> str:
    if alert_type.startswith("plugin"):
        return "plugin"
    if alert_type in ("broken_link", "performance"):
        return alert_type
    return "other"


@router.get("/summary", response_model=AlertSummary)
async def alert_summary(
    site_id: str | None = None, db: AsyncSession = Depends(get_db)
) -> AlertSummary:
    """Exact open/acknowledged alert counts — tab badges and pagination totals
    must not be derived from a row-capped list response."""
    query = (
        select(Alert.type, Alert.severity, func.count())
        .where(Alert.agent == "watchdog", Alert.status.in_(["open", "acknowledged"]))
        .group_by(Alert.type, Alert.severity)
    )
    if site_id:
        query = query.where(Alert.site_id == site_id)
    rows = (await db.execute(query)).all()

    total = 0
    by_type: dict[str, int] = {}
    by_severity: dict[str, int] = {}
    matrix: dict[str, dict[str, int]] = {}
    for alert_type, severity, count in rows:
        bucket = _bucket(alert_type)
        total += count
        by_type[bucket] = by_type.get(bucket, 0) + count
        by_severity[severity] = by_severity.get(severity, 0) + count
        matrix.setdefault(bucket, {})[severity] = matrix.get(bucket, {}).get(severity, 0) + count
    return AlertSummary(total=total, by_type=by_type, by_severity=by_severity, matrix=matrix)


@router.put("/alerts/{alert_id}/acknowledge")
async def acknowledge_alert(alert_id: str, db: AsyncSession = Depends(get_db)) -> dict[str, str]:
    result = await db.execute(select(Alert).where(Alert.id == alert_id))
    alert = result.scalar_one_or_none()
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    alert.status = "acknowledged"
    await db.flush()
    return {"status": "acknowledged"}


@router.put("/alerts/{alert_id}/dismiss")
async def dismiss_alert(alert_id: str, db: AsyncSession = Depends(get_db)) -> dict[str, str]:
    result = await db.execute(select(Alert).where(Alert.id == alert_id))
    alert = result.scalar_one_or_none()
    if not alert:
        raise HTTPException(status_code=404, detail="Alert not found")
    alert.status = "dismissed"
    alert.resolved_at = datetime.now(UTC)
    await db.flush()
    return {"status": "dismissed"}


class FlushRequest(BaseModel):
    site_id: str | None = None
    module: str | None = None  # "links" | "performance" | "plugins" | None (all)


async def _run_watchdog(site_id: str | None, module: str | None) -> None:
    """Re-run the watchdog agents and record the outcome.

    Nothing is deleted up front. Each agent already reconciles its own alerts
    (create / update in place / delete what it verified as fixed), so a
    pre-emptive delete only destroyed `created_at`, acknowledgements and
    dismissals — and, when the re-run then failed, left the page reporting
    "no issues found" as though the sites were healthy.
    """
    import logging

    from app.agents.watchdog.link_checker import LinkChecker
    from app.agents.watchdog.performance import PerformanceMonitor
    from app.agents.watchdog.plugin_audit import PluginAuditor
    from app.services.job_executor import AGENT_TIMEOUTS
    from app.services.site_scope import select_monitored_sites

    logger = logging.getLogger(__name__)

    failures: list[str] = []
    ran = 0

    async with AsyncSessionLocal() as db:
        result = await db.execute(select_monitored_sites())
        sites = result.scalars().all()
        if site_id:
            sites = [s for s in sites if s.id == site_id]

        agents_map = {
            "links": [LinkChecker],
            "performance": [PerformanceMonitor],
            "plugins": [PluginAuditor],
        }
        agent_classes = agents_map.get(module or "", [LinkChecker, PerformanceMonitor, PluginAuditor])

        for site in sites:
            for AgentClass in agent_classes:
                name = AgentClass.__name__
                try:
                    agent = AgentClass(db)
                    # Bounded per agent — a hung crawl must not stall the re-run
                    await asyncio.wait_for(
                        agent.run(site.id), timeout=AGENT_TIMEOUTS.get(name, 300)
                    )
                    # Commit per agent so one failure doesn't discard the rest,
                    # and so re-run results actually persist (BaseAgent only flushes).
                    await db.commit()
                    ran += 1
                except Exception as exc:
                    await db.rollback()
                    failures.append(f"{name} on {site.name}: {exc}")
                    logger.warning("Watchdog %s failed for site %s: %s", name, site.id, exc)

        await _record_run(db, ran=ran, failures=failures)
        await db.commit()


async def _record_run(db: AsyncSession, *, ran: int, failures: list[str]) -> None:
    """Persist the last re-run outcome so a failed background run is visible.

    Without this the only trace of a failure was a server-side log line, and
    the UI rendered an empty alert list as "Your sites are healthy."
    """
    from app.services.app_settings import set_json_setting

    await set_json_setting(
        db,
        _RUN_STATUS_KEY,
        {
            "finished_at": datetime.now(UTC).isoformat(),
            "agents_succeeded": ran,
            # Bounded: this is a status banner, not a log sink.
            "failures": failures[:10],
            "failure_count": len(failures),
        },
    )


@router.get("/last-run")
async def last_run(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Outcome of the most recent re-run, so the UI can distinguish
    "genuinely healthy" from "the re-run crashed"."""
    from app.services.app_settings import get_json_setting

    return await get_json_setting(db, _RUN_STATUS_KEY)


@router.post("/flush", dependencies=[Depends(job_limiter)])
async def flush_watchdog(
    req: FlushRequest, background_tasks: BackgroundTasks, db: AsyncSession = Depends(get_db)
) -> dict[str, str]:
    """Re-run the watchdog agents against current reality.

    Deliberately non-destructive: the agents reconcile their own alerts, so
    existing rows keep their first-seen time and their acknowledged/dismissed
    state, and a re-run that fails leaves the previous findings on screen
    instead of replacing them with a false all-clear.
    """
    await _record_run(db, ran=0, failures=[])  # clears the previous banner
    background_tasks.add_task(_run_watchdog, req.site_id, req.module)
    return {"status": "running", "message": "Re-running watchdog in background"}
