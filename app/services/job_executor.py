"""
Detached background job executor for on-demand agent runs.

execute_job(job_id) is fired via asyncio.create_task() — it is NEVER awaited
by the HTTP request handler. It opens its own DB session and outlives the
request context entirely.
"""
import asyncio
import importlib
import logging
from datetime import UTC, datetime

from sqlalchemy import select

from app.database.engine import AsyncSessionLocal
from app.database.models import AgentJob, AgentJobStep

logger = logging.getLogger(__name__)

# Imported here to avoid circular imports — same values as agents.py
AGENT_STEPS = [
    ("app.agents.optimizer.content_scorer",  "ContentScorer",    "Content Scorer",       "optimizer"),
    ("app.agents.optimizer.seo_analyzer",    "SEOAnalyzer",      "SEO Analyzer",         "optimizer"),
    ("app.agents.optimizer.internal_linker", "InternalLinker",   "Internal Link Finder", "optimizer"),
    ("app.agents.watchdog.plugin_audit",     "PluginAuditor",    "Plugin Auditor",       "watchdog"),
    ("app.agents.watchdog.performance",      "PerformanceMonitor","Performance Monitor",  "watchdog"),
    ("app.agents.watchdog.link_checker",     "LinkChecker",      "Link Checker",         "watchdog"),
    ("app.agents.autopilot.repurposer",      "ContentRepurposer","Content Repurposer",   "autopilot"),
    ("app.agents.flows.flow_classifier",     "FlowClassifier",   "Flow Classifier",      "flows"),
]

AGENT_MODULES: dict[str, str] = {class_name: module_path for module_path, class_name, _, _ in AGENT_STEPS}

AGENT_TIMEOUTS: dict[str, int] = {
    # ContentScorer commits progress incrementally (every CONTENT_COMMIT_EVERY
    # items, and per AI-recommendation chunk) and caps how much it attempts
    # per run (CONTENT_ANALYSIS_BATCH_SIZE) — so on an enterprise-scale site,
    # hitting this timeout now just means "the rest continues next run",
    # not "everything analyzed so far is silently discarded".
    "ContentScorer":     600,
    "SEOAnalyzer":        60,
    "InternalLinker":     150,  # bounded-concurrency live WordPress content fetches + GSC page-query lookups, to verify anchors
    "PluginAuditor":      180,
    "PerformanceMonitor": 300,
    "LinkChecker":        600,  # up to LINK_CHECK_MAX_URLS links per run
    "ContentRepurposer":  180,
    "FlowClassifier":      60,  # one GA4 funnel query per active flow category
}


def _now() -> datetime:
    return datetime.now(UTC)


async def _safe_rollback(db) -> None:
    """Reset the session after a failed/cancelled agent.

    Without this, one flush failure poisons the session and every subsequent
    step dies with "transaction has been rolled back" instead of running.
    """
    try:
        await db.rollback()
    except Exception:
        logger.exception("Session rollback failed")


async def execute_job(job_id: str) -> None:
    """
    Run this job's own agents sequentially — whichever subset was selected at
    creation time (see AgentJobStep rows), not a fixed global list — persisting
    step progress to DB after every agent so the SSE poller can stream live
    updates to the client.

    Uses its own AsyncSessionLocal session — never shares with any HTTP request.
    """
    async with AsyncSessionLocal() as db:
        try:
            job = await db.get(AgentJob, job_id)
            if job is None:
                logger.error("execute_job: job %s not found in DB", job_id)
                return

            job.status = "running"
            job.started_at = _now()
            await db.commit()

            steps_result = await db.execute(
                select(AgentJobStep)
                .where(AgentJobStep.job_id == job_id)
                .order_by(AgentJobStep.step_index)
            )
            job_steps = steps_result.scalars().all()

            for step in job_steps:
                class_name = step.agent_name
                module_path = AGENT_MODULES.get(class_name)

                # Re-read job row to pick up stop_requested written by a stop endpoint
                await db.refresh(job)
                if job.stop_requested:
                    job.status = "stopped"
                    job.completed_at = _now()
                    await db.commit()
                    logger.info("Job %s stopped before agent %s", job_id, class_name)
                    return

                step.status = "running"
                step.started_at = _now()
                await db.commit()

                timeout = AGENT_TIMEOUTS.get(class_name, 120)
                step_status, step_error, alerts_count = "done", None, 0
                try:
                    if module_path is None:
                        raise RuntimeError(f"Unknown agent {class_name!r} — no module registered")
                    mod = importlib.import_module(module_path)
                    AgentClass = getattr(mod, class_name)
                    agent = AgentClass(db)
                    alerts = await asyncio.wait_for(agent.run(job.site_id), timeout=timeout)
                    alerts_count = len(alerts)
                    logger.info("Job %s step %s done — %d alerts", job_id, class_name, alerts_count)
                except TimeoutError:
                    # Cancellation can land mid-flush — the session MUST be
                    # rolled back before the step result is written, or the
                    # whole job dies on the next commit.
                    await _safe_rollback(db)
                    step_status = "error"
                    step_error = f"Timed out after {timeout}s"
                    logger.warning("Job %s step %s timed out (%ds)", job_id, class_name, timeout)
                except Exception as exc:
                    await _safe_rollback(db)
                    step_status = "error"
                    step_error = (str(exc) or type(exc).__name__)[:500]
                    logger.warning("Job %s step %s failed: %s", job_id, class_name, exc)

                step.status = step_status
                step.error_message = step_error
                step.alerts_count = alerts_count
                step.completed_at = _now()
                await db.commit()  # persist step result immediately so poller sees it

            # Final job status
            await db.refresh(job)
            job.status = "stopped" if job.stop_requested else "done"
            job.completed_at = _now()
            await db.commit()
            logger.info("Job %s finished with status %s", job_id, job.status)

        except Exception as exc:
            logger.error("execute_job fatal error for job %s: %s", job_id, exc, exc_info=True)
            try:
                await _safe_rollback(db)
                async with AsyncSessionLocal() as recovery_db:
                    job = await recovery_db.get(AgentJob, job_id)
                    if job:
                        job.status = "error"
                        job.error_message = (str(exc) or type(exc).__name__)[:500]
                        job.completed_at = _now()
                    # No step may be left "running" on a dead job
                    stuck = (await recovery_db.execute(
                        select(AgentJobStep).where(
                            AgentJobStep.job_id == job_id,
                            AgentJobStep.status == "running",
                        )
                    )).scalars().all()
                    for s in stuck:
                        s.status = "error"
                        s.error_message = "Job aborted"
                        s.completed_at = _now()
                    await recovery_db.commit()
            except Exception:
                logger.exception("Could not mark job %s as errored", job_id)
