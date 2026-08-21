"""Re-measuring one page's performance on demand, and doing it to many.

The scheduled sweep rotates: each run takes a bounded, least-recently-measured
slice, so a page someone is actively working on can be days away from its turn
and a fix made ten minutes ago is invisible until then. This is the manual
override — measure this page now, or re-measure every page currently reported,
without waiting for the rotation to come round.

The per-page work is not reimplemented here. `measure_page` and `classify`
come from the agent, so a hand-triggered measurement and a scheduled one can
never disagree about what a score means or how it is worded.

Nothing here holds a database connection across a PageSpeed call. A single
measurement is 10-30 seconds of network time, and a connection parked on a
socket wait is one no request can have.
"""
import asyncio
import logging
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.watchdog.performance import (
    Measurement,
    classify,
    measure_page,
    psi_concurrency,
    snapshot_for,
)
from app.config import settings
from app.database.engine import AsyncSessionLocal
from app.database.models import Alert, ContentPost, PerformanceSnapshot, Site

logger = logging.getLogger(__name__)

STATUS_KEY = "watchdog.performance_rescan"

# A run with no progress for this long is treated as stalled rather than
# running. Nothing else can tell the difference: the worker writes its
# `finished` record in a `finally`, which a killed process never reaches — so a
# reload, a crash or a deploy mid-run leaves `running: true` forever, the UI
# polling a number that will never move, and the "already running" guard
# blocking every new run. A page takes about thirty seconds and progress is
# saved after each, so three minutes of silence is unambiguous.
STALE_AFTER = timedelta(minutes=3)

# Scopes a caller may ask for. "reported" is the set the Performance tab
# lists — re-running "the result" means exactly those pages. "all" is every
# tracked page, bounded by PSI_RESCAN_MAX_PAGES.
SCOPES = ("reported", "all")


@dataclass
class RescanProgress:
    total: int = 0
    done: int = 0
    failed: int = 0
    # Pages that came back healthy and had their alert cleared. Worth its own
    # number: "12 measured" and "12 measured, 5 now fixed" are different news.
    resolved: int = 0
    running: bool = False
    started_at: str | None = None
    finished_at: str | None = None
    site_id: str | None = None
    scope: str | None = None
    # Heartbeat. Written on every save so a dead run is distinguishable from a
    # slow one — see STALE_AFTER.
    updated_at: str | None = None
    # Set by the stop endpoint; the worker checks it between pages. A run of
    # two hundred pages is nearly an hour, which is far too long to be
    # committed to by one click.
    stop_requested: bool = False
    stopped: bool = False
    # Bounded: this drives a status banner, not an audit log.
    failures: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


async def _save(progress: RescanProgress) -> None:
    from app.services.app_settings import set_json_setting

    progress.updated_at = datetime.now(UTC).isoformat()
    async with AsyncSessionLocal() as db:
        await set_json_setting(db, STATUS_KEY, progress.as_dict())
        await db.commit()


def is_stalled(record: dict[str, Any]) -> bool:
    """Whether a record claiming to run has actually stopped reporting."""
    if not record.get("running"):
        return False
    stamp = record.get("updated_at") or record.get("started_at")
    if not stamp:
        return True
    try:
        last = datetime.fromisoformat(stamp)
    except ValueError:
        return True
    return datetime.now(UTC) - last > STALE_AFTER


def is_active(record: dict[str, Any]) -> bool:
    """Running *and* still reporting — the only state that blocks a new run."""
    return bool(record.get("running")) and not is_stalled(record)


async def request_stop() -> bool:
    """Ask the worker to stop after the page it is on.

    Also the way out of a stalled run: the record is marked stopped so the UI
    reports it as over and the guard lets a new run start.
    """
    from app.services.app_settings import get_json_setting, set_json_setting

    async with AsyncSessionLocal() as db:
        record = await get_json_setting(db, STATUS_KEY)
        if not record.get("running"):
            return False
        record["stop_requested"] = True
        if is_stalled(record):
            # Nothing is left to notice the request, so close it out here.
            record.update(running=False, stopped=True,
                          finished_at=datetime.now(UTC).isoformat())
        await set_json_setting(db, STATUS_KEY, record)
        await db.commit()
        return True


async def _stop_requested() -> bool:
    from app.services.app_settings import get_json_setting

    async with AsyncSessionLocal() as db:
        return bool((await get_json_setting(db, STATUS_KEY)).get("stop_requested"))


async def read_progress() -> dict[str, Any]:
    from app.services.app_settings import get_json_setting

    async with AsyncSessionLocal() as db:
        return await get_json_setting(db, STATUS_KEY)


async def known_urls(db: AsyncSession, site: Site) -> set[str]:
    """Every URL this site legitimately owns.

    The measure endpoint takes a URL from the client and makes the server
    fetch it, so the URL has to be one the site already tracks — its
    homepage, a synced page, or a page an existing alert already names.
    Validating "is it public" would still leave this a general-purpose
    fetcher pointed at anything on the internet.
    """
    urls: set[str] = {site.url, site.url.rstrip("/")}

    tracked = (await db.execute(
        select(ContentPost.url).where(
            ContentPost.site_id == site.id, ContentPost.url.isnot(None)
        )
    )).scalars().all()
    urls.update(u for u in tracked if u)

    alerts = (await db.execute(
        select(Alert.metadata_).where(
            Alert.site_id == site.id,
            Alert.agent == "watchdog",
            Alert.type == "performance",
        )
    )).scalars().all()
    urls.update(
        page_url for meta in alerts
        if isinstance(meta, dict) and (page_url := meta.get("page_url"))
    )

    return urls


def order_by_staleness(
    candidates: list[str], last_seen: dict[str, datetime]
) -> list[str]:
    """Most-stale first, never-measured ahead of everything.

    Ordering matters because the batch is capped: taking the
    least-recently-measured first means pressing the button again reaches
    pages the last batch could not, rather than re-measuring the same head of
    the list forever. The incoming order survives as the tie-break, so among
    equally stale pages the busiest still goes first.
    """
    far_past = datetime.min.replace(tzinfo=UTC)
    ordered = list(dict.fromkeys(u for u in candidates if u))
    ordered.sort(key=lambda u: last_seen.get(u) or far_past)
    return ordered


async def select_scope(db: AsyncSession, site: Site, scope: str) -> list[str]:
    """Candidate URLs for a bulk re-measure, most-stale first."""
    if scope == "reported":
        rows = (await db.execute(
            select(Alert.metadata_).where(
                Alert.site_id == site.id,
                Alert.agent == "watchdog",
                Alert.type == "performance",
                Alert.status.in_(["open", "acknowledged"]),
            )
        )).scalars().all()
        candidates = [
            page_url for meta in rows
            if isinstance(meta, dict) and (page_url := meta.get("page_url"))
        ]
    else:
        tracked = (await db.execute(
            select(ContentPost.url)
            .where(ContentPost.site_id == site.id, ContentPost.url.isnot(None))
            .order_by(ContentPost.traffic_30d.desc())
        )).scalars().all()
        candidates = [site.url, *(u for u in tracked if u)]

    seen_rows = (await db.execute(
        select(
            PerformanceSnapshot.page_url,
            PerformanceSnapshot.snapshot_at,
        ).where(PerformanceSnapshot.site_id == site.id)
    )).all()
    last_seen: dict[str, datetime] = {}
    for page_url, snapshot_at in seen_rows:
        if page_url not in last_seen or snapshot_at > last_seen[page_url]:
            last_seen[page_url] = snapshot_at

    return order_by_staleness(candidates, last_seen)


async def apply_measurement(
    db: AsyncSession, site_id: str, m: Measurement
) -> dict[str, Any]:
    """Record a measurement and reconcile this page's alert.

    Same reconciliation the sweep performs, for one page: the alert is
    updated in place so `created_at` still means "first seen" and an
    acknowledgement survives, and a page that now scores well has its alert
    deleted rather than left on screen as a problem someone already fixed.
    """
    from app.agents.watchdog.performance import PerformanceMonitor

    verdict = classify(m)
    if m.error is None:
        db.add(snapshot_for(site_id, m))

    rows = (await db.execute(
        select(Alert).where(
            Alert.site_id == site_id,
            Alert.agent == "watchdog",
            Alert.type == "performance",
        )
    )).scalars().all()
    matching = [a for a in rows if (a.metadata_ or {}).get("page_url") == m.url]

    agent = PerformanceMonitor(db)
    resolved = False

    if verdict.severity is None:
        if matching:
            await db.execute(delete(Alert).where(Alert.id.in_([a.id for a in matching])))
            resolved = True
    elif matching:
        # Duplicates predate the page_url alert identity; keep the oldest so
        # first-seen and any acknowledgement survive, drop the rest.
        matching.sort(key=lambda a: a.created_at)
        keep, extra = matching[0], matching[1:]
        await agent.update_alert(
            keep, severity=verdict.severity, title=verdict.title,
            description=verdict.description, metadata=verdict.metadata,
        )
        if extra:
            await db.execute(delete(Alert).where(Alert.id.in_([a.id for a in extra])))
    else:
        await agent.create_alert(
            site_id=site_id, agent="watchdog", severity=verdict.severity,
            type_="performance", title=verdict.title,
            description=verdict.description, metadata=verdict.metadata,
        )

    await db.flush()
    return {
        "url": m.url,
        "severity": verdict.severity,
        "resolved": resolved,
        # The alert's own sentence, so the caller reports what was measured in
        # the same words the alert does rather than reassembling it from the
        # metadata and getting the unreachable case wrong.
        "description": verdict.description,
        "measured_at": datetime.now(UTC).isoformat(),
        **verdict.metadata,
    }


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        limits=httpx.Limits(max_connections=4, max_keepalive_connections=2)
    )


async def remeasure_one(site_id: str, url: str) -> dict[str, Any]:
    """Measure one page now and reconcile it, in that order.

    The measurement happens before any session is opened so the PageSpeed
    round-trip does not sit on a pooled connection.
    """
    async with _client() as client:
        m = await measure_page(client, url)

    async with AsyncSessionLocal() as db:
        result = await apply_measurement(db, site_id, m)
        await db.commit()
    return result


async def run_bulk_remeasure(site_id: str, urls: list[str], scope: str) -> None:
    """Re-measure a batch, reporting progress as it goes.

    Each page gets its own session and commit, so one failure costs one page
    and partial progress survives a restart instead of rolling back the lot.
    """
    progress = RescanProgress(
        total=len(urls), running=True, site_id=site_id, scope=scope,
        started_at=datetime.now(UTC).isoformat(),
    )
    await _save(progress)

    semaphore = asyncio.Semaphore(psi_concurrency())
    lock = asyncio.Lock()

    async def one(client: httpx.AsyncClient, url: str) -> None:
        outcome, message, resolved = "done", None, False
        async with semaphore:
            # Checked here rather than only before the gather: a two-hundred
            # page run is nearly an hour, so a stop has to take effect within
            # one page, not at the end of the batch. Pages already in flight
            # finish — abandoning a measurement mid-request would leave the
            # page counted as neither done nor failed.
            if progress.stop_requested or await _stop_requested():
                progress.stop_requested = True
                return
            m = await measure_page(client, url)
            async with AsyncSessionLocal() as db:
                try:
                    result = await apply_measurement(db, site_id, m)
                    await db.commit()
                    resolved = bool(result.get("resolved"))
                except Exception as exc:
                    await db.rollback()
                    outcome, message = "failed", f"{url}: {exc}"
                    logger.warning("Performance re-measure failed for %s: %s", url, exc)

        async with lock:
            if outcome == "done":
                progress.done += 1
                if resolved:
                    progress.resolved += 1
            else:
                progress.failed += 1
            if message and len(progress.failures) < 10:
                progress.failures.append(message)
            # Persisted as it goes: a sweep of 200 pages is half an hour, and
            # a spinner that reveals nothing for that long reads as a hang.
            await _save(progress)

    try:
        async with _client() as client:
            await asyncio.gather(*(one(client, u) for u in urls))
    finally:
        # Best effort only. A killed process never reaches this, which is why
        # the record carries a heartbeat and `is_stalled` exists — without it
        # a reload mid-run left `running: true` forever and the UI polled a
        # number that would never move again.
        progress.running = False
        progress.stopped = progress.stop_requested
        progress.finished_at = datetime.now(UTC).isoformat()
        await _save(progress)
        if progress.stopped:
            logger.info(
                "Performance re-measure stopped on request after %d of %d pages",
                progress.done + progress.failed, progress.total,
            )


def rescan_ceiling() -> int:
    return max(1, settings.PSI_RESCAN_MAX_PAGES)


__all__ = [
    "SCOPES",
    "STATUS_KEY",
    "RescanProgress",
    "apply_measurement",
    "known_urls",
    "order_by_staleness",
    "read_progress",
    "remeasure_one",
    "rescan_ceiling",
    "run_bulk_remeasure",
    "select_scope",
]
