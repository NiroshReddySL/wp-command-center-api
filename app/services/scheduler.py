"""APScheduler — runs all agents on a schedule.

Each site runs in its own session and commits independently, so one failing
site never discards another site's results. Every job pins `max_instances=1`
(no overlapping self-runs) and a generous `misfire_grace_time` so a busy
event loop at the trigger instant delays a daily job instead of skipping it.
"""
import logging
from collections.abc import Awaitable, Callable

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.engine import AsyncSessionLocal
from app.database.models import Site

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler(timezone="UTC")


async def _active_site_ids() -> list[str]:
    """Every monitored site — see `site_scope`. Deliberately NOT
    `status == "active"`: that excluded any site whose last content sync
    failed, which is the one state where monitoring matters most."""
    from app.services.site_scope import select_monitored_site_ids

    async with AsyncSessionLocal() as db:
        result = await db.execute(select_monitored_site_ids())
        return [row[0] for row in result.all()]


async def _toggles() -> dict[str, bool]:
    """Agent enable/disable state from Settings — gates scheduled runs only."""
    from app.services.app_settings import get_agent_toggles

    async with AsyncSessionLocal() as db:
        return await get_agent_toggles(db)


async def _run_per_site(
    job_name: str,
    site_work: Callable[[AsyncSession, str], Awaitable[None]],
) -> None:
    """Run `site_work` for every active site — fresh session + commit per site."""
    site_ids = await _active_site_ids()
    failures = 0
    for site_id in site_ids:
        async with AsyncSessionLocal() as db:
            try:
                await site_work(db, site_id)
                await db.commit()
            except Exception as exc:
                await db.rollback()
                failures += 1
                logger.error("%s failed for site %s: %s", job_name, site_id, exc)
    logger.info("%s finished: %d site(s), %d failure(s)", job_name, len(site_ids), failures)


async def run_watchdog() -> None:
    from app.agents.watchdog.link_checker import LinkChecker
    from app.agents.watchdog.plugin_audit import PluginAuditor

    toggles = await _toggles()
    run_links = toggles["watchdog.links"]
    run_plugins = toggles["watchdog.plugins"]
    if not (run_links or run_plugins):
        logger.info("Scheduler: Watchdog skipped (disabled in Settings)")
        return
    logger.info("Scheduler: running Watchdog (links=%s, plugins=%s)", run_links, run_plugins)

    async def work(db: AsyncSession, site_id: str) -> None:
        if run_links:
            await LinkChecker(db).run(site_id)
        if run_plugins:
            await PluginAuditor(db).run(site_id)

    await _run_per_site("Watchdog", work)


async def run_performance_checks() -> None:
    from app.agents.watchdog.performance import PerformanceMonitor

    if not (await _toggles())["watchdog.performance"]:
        logger.info("Scheduler: Performance skipped (disabled in Settings)")
        return
    logger.info("Scheduler: running Performance checks")

    async def work(db: AsyncSession, site_id: str) -> None:
        await PerformanceMonitor(db).run(site_id)

    await _run_per_site("Performance", work)


async def run_optimizer() -> None:
    from app.agents.optimizer.content_scorer import ContentScorer
    from app.agents.optimizer.internal_linker import InternalLinker
    from app.agents.optimizer.seo_analyzer import SEOAnalyzer

    toggles = await _toggles()
    run_content = toggles["optimizer.content"]
    run_seo = toggles["optimizer.seo"]
    if not (run_content or run_seo):
        logger.info("Scheduler: Optimizer skipped (disabled in Settings)")
        return
    logger.info("Scheduler: running Optimizer (content=%s, seo=%s)", run_content, run_seo)

    async def work(db: AsyncSession, site_id: str) -> None:
        if run_content:
            await ContentScorer(db).run(site_id)
        if run_seo:
            await SEOAnalyzer(db).run(site_id)
            await InternalLinker(db).run(site_id)

    await _run_per_site("Optimizer", work)


async def run_traffic_agent() -> None:
    from app.agents.traffic.traffic_agent import TrafficAgent

    if not (await _toggles())["traffic.sync"]:
        logger.info("Scheduler: Traffic skipped (disabled in Settings)")
        return
    logger.info("Scheduler: running Traffic agent")

    async def work(db: AsyncSession, site_id: str) -> None:
        await TrafficAgent(db).run(site_id)

    await _run_per_site("Traffic", work)


async def run_traffic_predictions() -> None:
    from app.services.traffic_prediction import TrafficPredictionService

    if not (await _toggles())["traffic.sync"]:
        logger.info("Scheduler: Traffic predictions skipped (disabled in Settings)")
        return
    logger.info("Scheduler: running Traffic predictions")

    async def work(db: AsyncSession, site_id: str) -> None:
        svc = TrafficPredictionService(db)
        for horizon in [7, 14, 30]:
            await svc.get_or_generate(site_id, horizon, force=True)

    await _run_per_site("TrafficPredictions", work)


async def run_flow_classifier() -> None:
    from app.agents.flows.flow_classifier import FlowClassifier

    if not (await _toggles())["flows.classify"]:
        logger.info("Scheduler: Flow classifier skipped (disabled in Settings)")
        return
    logger.info("Scheduler: running Flow classifier")

    async def work(db: AsyncSession, site_id: str) -> None:
        await FlowClassifier(db).run(site_id)

    await _run_per_site("FlowClassifier", work)


async def run_weekly_reports() -> None:
    from app.agents.autopilot.reporter import Reporter

    if not (await _toggles())["autopilot.reports"]:
        logger.info("Scheduler: Reporter skipped (disabled in Settings)")
        return
    logger.info("Scheduler: generating weekly reports")

    async def work(db: AsyncSession, site_id: str) -> None:
        await Reporter(db).run(site_id)

    await _run_per_site("Reporter", work)
    await _send_weekly_digest()


async def _send_weekly_digest() -> None:
    """Post a 'weekly reports ready' card to Teams if the digest pref is on."""
    from app.config import settings as app_config
    from app.services.app_settings import get_notification_prefs
    from app.services.notification import build_digest_card, send_teams_message

    async with AsyncSessionLocal() as db:
        prefs = await get_notification_prefs(db)
        webhook_url = prefs["teams_webhook_url"] or app_config.TEAMS_WEBHOOK_URL
        if not webhook_url or not prefs["weekly_digest"]:
            return
        from app.services.site_scope import monitored

        # Same scope as the agent runs: a site whose sync failed still has a
        # health score worth reporting — omitting it hides the bad news.
        result = await db.execute(
            select(Site.name, Site.health_score).where(monitored()).order_by(Site.name)
        )
        site_rows = [(name, round(score or 0)) for name, score in result.all()]
    if not site_rows:
        return
    try:
        await send_teams_message(webhook_url, build_digest_card(site_rows))
    except Exception as exc:
        logger.error("Weekly digest Teams post failed: %s", exc)


def setup_scheduler() -> None:
    if not settings.ENABLE_SCHEDULER:
        # Multi-worker deployments must run the scheduler in exactly one
        # process — start the others with ENABLE_SCHEDULER=false.
        logger.info("Scheduler disabled via ENABLE_SCHEDULER=false")
        return

    common = {"replace_existing": True, "max_instances": 1, "coalesce": True}

    scheduler.add_job(
        run_watchdog,
        trigger=IntervalTrigger(hours=6),
        id="watchdog",
        misfire_grace_time=300,
        **common,
    )
    scheduler.add_job(
        run_performance_checks,
        trigger=IntervalTrigger(hours=2),
        id="performance",
        misfire_grace_time=120,
        **common,
    )
    scheduler.add_job(
        run_optimizer,
        trigger=CronTrigger(hour=3, minute=0),
        id="optimizer",
        misfire_grace_time=3600,
        **common,
    )
    scheduler.add_job(
        run_traffic_agent,
        trigger=CronTrigger(hour=1, minute=0),   # 1 AM UTC daily — after GA processes previous day
        id="traffic",
        misfire_grace_time=3600,
        **common,
    )
    scheduler.add_job(
        run_traffic_predictions,
        trigger=CronTrigger(hour=2, minute=0),   # 2 AM UTC daily — after traffic snapshots
        id="traffic_predictions",
        misfire_grace_time=3600,
        **common,
    )
    scheduler.add_job(
        run_flow_classifier,
        trigger=CronTrigger(hour=4, minute=0),   # 4 AM UTC daily — after GA4 has processed "yesterday"
        id="flow_classifier",
        misfire_grace_time=3600,
        **common,
    )
    scheduler.add_job(
        run_weekly_reports,
        trigger=CronTrigger(day_of_week="fri", hour=6, minute=0),
        id="reporter",
        misfire_grace_time=3600,
        **common,
    )

    scheduler.start()
    logger.info(
        "Scheduler started: watchdog(6h), performance(2h), optimizer(3am), traffic(1am), "
        "predictions(2am), flows(4am), reporter(Fri 6am)"
    )
