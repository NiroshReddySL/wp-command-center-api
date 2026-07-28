"""Manual agent trigger endpoint — runs a chosen subset of agents for a site on demand."""
import asyncio
import json
import logging
from datetime import datetime
from typing import Any, AsyncGenerator

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.engine import AsyncSessionLocal, get_db
from app.security.rate_limit import job_limiter
from app.database.models import AgentJob, AgentJobStep, Site
from app.services.job_executor import execute_job

router = APIRouter()
logger = logging.getLogger(__name__)


# ── Pydantic schemas ──────────────────────────────────────────────────────────

class JobStepOut(BaseModel):
    id: str
    step_index: int
    agent_name: str
    label: str
    category: str
    status: str
    alerts_count: int
    error_message: str | None
    started_at: datetime | None
    completed_at: datetime | None

    model_config = {"from_attributes": True}


class JobOut(BaseModel):
    id: str
    site_id: str
    status: str
    stop_requested: bool
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    steps: list[JobStepOut]

    model_config = {"from_attributes": True}

# Per-site cancellation flags — set by POST /{site_id}/stop, cleared on stream start/end
_stop_flags: dict[str, bool] = {}

# Ordered list of (AgentClass import path, display label, category)
AGENT_STEPS = [
    ("app.agents.optimizer.content_scorer", "ContentScorer",   "Content Scorer",      "optimizer"),
    ("app.agents.optimizer.seo_analyzer",   "SEOAnalyzer",     "SEO Analyzer",        "optimizer"),
    ("app.agents.optimizer.internal_linker","InternalLinker",  "Internal Link Finder","optimizer"),
    ("app.agents.watchdog.plugin_audit",    "PluginAuditor",   "Plugin Auditor",      "watchdog"),
    ("app.agents.watchdog.performance",     "PerformanceMonitor","Performance Monitor","watchdog"),
    ("app.agents.watchdog.link_checker",    "LinkChecker",     "Link Checker",        "watchdog"),
    ("app.agents.autopilot.repurposer",     "ContentRepurposer","Content Repurposer", "autopilot"),
    ("app.agents.flows.flow_classifier",    "FlowClassifier",  "Flow Classifier",     "flows"),
]

# Maps a manual-run agent (by class name) to the Agent Configuration toggle
# that gates its SCHEDULED run — used only to pick sensible defaults for the
# manual run picklist. InternalLinker shares SEOAnalyzer's toggle because the
# scheduler runs them as a pair; ContentRepurposer has no scheduled
# counterpart at all, so it has no toggle to default from.
AGENT_TOGGLE_KEY: dict[str, str] = {
    "ContentScorer": "optimizer.content",
    "SEOAnalyzer": "optimizer.seo",
    "InternalLinker": "optimizer.seo",
    "PluginAuditor": "watchdog.plugins",
    "PerformanceMonitor": "watchdog.performance",
    "LinkChecker": "watchdog.links",
    "FlowClassifier": "flows.classify",
}


class ManualAgentOption(BaseModel):
    agent_name: str
    label: str
    category: str
    default_enabled: bool


class RunJobRequest(BaseModel):
    # None (or omitted) runs every agent — back-compat default for any other caller.
    agent_names: list[str] | None = None


def _select_steps(
    available: list[tuple[str, str, str, str]], agent_names: list[str] | None,
) -> list[tuple[str, str, str, str]]:
    """Filter AGENT_STEPS-shaped tuples down to the requested class names,
    preserving AGENT_STEPS order. `None` means "run everything"."""
    if agent_names is None:
        return available
    return [s for s in available if s[1] in agent_names]


def _sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _compute_pct(steps: list) -> int:
    if not steps:
        return 0
    done = sum(1 for s in steps if s.status in ("done", "error"))
    return round(done / len(steps) * 100)


async def _poll_job_stream(job_id: str) -> AsyncGenerator[str, None]:
    """Poll DB every 1.5s and emit SSE diffs until job reaches terminal state."""
    last_job_status: str | None = None
    last_step_statuses: dict[int, str] = {}

    async with AsyncSessionLocal() as db:
        # Verify job exists
        job = await db.get(AgentJob, job_id)
        if job is None:
            yield _sse("error", {"message": f"Job {job_id} not found"})
            return

        # Real step count for THIS job — varies with how many agents were
        # selected at creation time, so it can never be a fixed constant.
        total_r = await db.execute(
            select(func.count()).select_from(AgentJobStep).where(AgentJobStep.job_id == job_id)
        )
        yield _sse("start", {"job_id": job_id, "total": total_r.scalar_one()})
        yield ": keepalive\n\n"

        while True:
            db.expire_all()
            result = await db.execute(
                select(AgentJob)
                .where(AgentJob.id == job_id)
                .options(selectinload(AgentJob.steps))
            )
            job = result.scalar_one_or_none()
            if job is None:
                yield _sse("error", {"message": "Job disappeared"})
                return

            pct = _compute_pct(job.steps)

            # Emit step changes
            for step in job.steps:
                prev = last_step_statuses.get(step.step_index)
                if prev != step.status:
                    last_step_statuses[step.step_index] = step.status
                    yield _sse("step", {
                        "index": step.step_index,
                        "label": step.label,
                        "category": step.category,
                        "status": step.status,
                        "alerts": step.alerts_count,
                        "error": step.error_message,
                        "pct": pct,
                    })

            # Emit job status changes
            if last_job_status != job.status:
                last_job_status = job.status
                yield _sse("job_status", {"status": job.status, "pct": pct})

            yield ": keepalive\n\n"

            if job.status in ("done", "error", "stopped"):
                if job.status == "done":
                    yield _sse("done", {"status": "completed", "pct": 100})
                elif job.status == "stopped":
                    yield _sse("stopped", {"pct": pct})
                else:
                    yield _sse("error", {"message": job.error_message or "Job failed"})
                return

            await asyncio.sleep(1.5)


async def run_agents_for_site(site_id: str) -> dict[str, int]:
    """Run all agents for a site using a fresh DB session. Returns alert counts."""
    counts: dict[str, int] = {}
    async with AsyncSessionLocal() as db:
        try:
            for module_path, class_name, _, _ in AGENT_STEPS:
                import importlib
                mod = importlib.import_module(module_path)
                AgentClass = getattr(mod, class_name)
                agent = AgentClass(db)
                alerts = await agent.run(site_id)
                counts[class_name] = len(alerts)
            await db.commit()
        except Exception as exc:
            logger.error("Agent run failed for site %s: %s", site_id, exc)
            await db.rollback()
            raise
    return counts


# Per-agent timeouts in seconds — generous but bounded
AGENT_TIMEOUTS: dict[str, int] = {
    "ContentScorer":    420,   # 7 min — parallel schema fetches + AI recs
    "SEOAnalyzer":       60,
    "InternalLinker":    150,  # bounded-concurrency live WordPress content fetches + GSC page-query lookups, to verify anchors
    "PluginAuditor":     180,
    "PerformanceMonitor":300,  # PSI calls can be slow
    "LinkChecker":       600,  # up to LINK_CHECK_MAX_URLS links per run
    "ContentRepurposer": 180,
}


async def _stream_agents(site_id: str) -> AsyncGenerator[str, None]:
    total = len(AGENT_STEPS)
    counts: dict[str, int] = {}

    # Clear any leftover stop flag from a previous run
    _stop_flags.pop(site_id, None)

    yield _sse("start", {"total": total, "site_id": site_id})

    async with AsyncSessionLocal() as db:
        try:
            for idx, (module_path, class_name, label, category) in enumerate(AGENT_STEPS):
                # Check cancellation before starting each agent
                if _stop_flags.get(site_id):
                    yield _sse("stopped", {
                        "index": idx,
                        "pct": round(idx / total * 100),
                        "message": f"Stopped before {label}",
                    })
                    await db.commit()
                    _stop_flags.pop(site_id, None)
                    return

                yield _sse("step", {
                    "index": idx,
                    "total": total,
                    "label": label,
                    "category": category,
                    "status": "running",
                    "pct": round(idx / total * 100),
                })
                # Keepalive: SSE comment lines prevent proxy/browser timeout during long agents
                yield ": keepalive\n\n"

                import importlib
                mod = importlib.import_module(module_path)
                AgentClass = getattr(mod, class_name)
                agent = AgentClass(db)

                timeout = AGENT_TIMEOUTS.get(class_name, 120)
                try:
                    alerts = await asyncio.wait_for(agent.run(site_id), timeout=timeout)
                    alert_count = len(alerts)
                    counts[class_name] = alert_count
                    yield _sse("step", {
                        "index": idx,
                        "total": total,
                        "label": label,
                        "category": category,
                        "status": "done",
                        "alerts": alert_count,
                        "pct": round((idx + 1) / total * 100),
                    })
                except asyncio.TimeoutError:
                    # Roll back — a cancellation mid-flush poisons the session
                    # and would kill every remaining step otherwise.
                    await db.rollback()
                    logger.warning("Agent %s timed out for site %s (limit %ds)", class_name, site_id, timeout)
                    yield _sse("step", {
                        "index": idx,
                        "total": total,
                        "label": label,
                        "category": category,
                        "status": "error",
                        "error": f"Timed out after {timeout}s",
                        "pct": round((idx + 1) / total * 100),
                    })
                except Exception as exc:
                    await db.rollback()
                    logger.warning("Agent %s failed for site %s: %s", class_name, site_id, exc)
                    yield _sse("step", {
                        "index": idx,
                        "total": total,
                        "label": label,
                        "category": category,
                        "status": "error",
                        "error": (str(exc) or type(exc).__name__)[:120],
                        "pct": round((idx + 1) / total * 100),
                    })

                yield ": keepalive\n\n"

            await db.commit()
            yield _sse("done", {"status": "completed", "counts": counts, "pct": 100})

        except Exception as exc:
            await db.rollback()
            logger.error("Agent stream failed for site %s: %s", site_id, exc)
            yield _sse("error", {"message": str(exc)})
        finally:
            _stop_flags.pop(site_id, None)


# ── Job-based routes (declare BEFORE /{site_id}/... to avoid route collision) ──

@router.get("/manual-options", response_model=list[ManualAgentOption])
async def list_manual_options(db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    """Agents selectable for a manual run, default-checked to match each
    agent's Agent Configuration toggle (checked with no opinion if it has none)."""
    from app.services.app_settings import get_agent_toggles

    toggles = await get_agent_toggles(db)
    return [
        {
            "agent_name": class_name,
            "label": label,
            "category": category,
            "default_enabled": toggles.get(AGENT_TOGGLE_KEY.get(class_name, ""), True),
        }
        for _, class_name, label, category in AGENT_STEPS
    ]


@router.post("/{site_id}/run-job", dependencies=[Depends(job_limiter)])
async def create_job(
    site_id: str, payload: RunJobRequest | None = None, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """Create a DB-backed AgentJob for the requested agents, fire detached
    executor task, return job_id immediately."""
    from app.services.job_executor import AGENT_STEPS as EXEC_STEPS

    result = await db.execute(select(Site).where(Site.id == site_id))
    site = result.scalar_one_or_none()
    if not site:
        raise HTTPException(status_code=404, detail="Site not found")

    agent_names = payload.agent_names if payload else None
    steps_to_run = _select_steps(EXEC_STEPS, agent_names)
    if not steps_to_run:
        raise HTTPException(status_code=400, detail="No agents selected")

    job = AgentJob(site_id=site_id)
    db.add(job)
    await db.flush()  # get job.id before creating steps

    for idx, (_, class_name, label, category) in enumerate(steps_to_run):
        step = AgentJobStep(
            job_id=job.id,
            step_index=idx,
            agent_name=class_name,
            label=label,
            category=category,
        )
        db.add(step)

    await db.commit()

    # Fire detached task — never awaited, outlives this request
    asyncio.create_task(execute_job(job.id))

    return {"job_id": job.id, "status": "pending"}


@router.get("/jobs/{job_id}/stream")
async def stream_job(job_id: str) -> StreamingResponse:
    return StreamingResponse(
        _poll_job_stream(job_id),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/jobs/{job_id}/stop")
async def stop_job(job_id: str, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    job = await db.get(AgentJob, job_id)
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    job.stop_requested = True
    await db.commit()
    return {"status": "stop_requested", "job_id": job_id}


@router.get("/jobs/{job_id}", response_model=JobOut)
async def get_job(job_id: str, db: AsyncSession = Depends(get_db)) -> AgentJob:
    result = await db.execute(
        select(AgentJob).where(AgentJob.id == job_id).options(selectinload(AgentJob.steps))
    )
    job = result.scalar_one_or_none()
    if not job:
        raise HTTPException(status_code=404, detail="Job not found")
    return job


@router.get("/{site_id}/run-stream")
async def stream_agents(site_id: str, db: AsyncSession = Depends(get_db)) -> StreamingResponse:
    result = await db.execute(select(Site).where(Site.id == site_id))
    site = result.scalar_one_or_none()
    if not site:
        raise HTTPException(status_code=404, detail="Site not found")

    return StreamingResponse(
        _stream_agents(site_id),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/{site_id}/stop")
async def stop_agents(site_id: str) -> dict[str, Any]:
    """Signal the running agent stream to stop after the current agent finishes."""
    _stop_flags[site_id] = True
    return {"status": "stop_requested", "site_id": site_id}


@router.post("/{site_id}/run", dependencies=[Depends(job_limiter)])
async def trigger_agents(site_id: str, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    result = await db.execute(select(Site).where(Site.id == site_id))
    site = result.scalar_one_or_none()
    if not site:
        raise HTTPException(status_code=404, detail="Site not found")

    counts = await run_agents_for_site(site_id)
    return {"status": "completed", "site_id": site_id, "counts": counts}
