import asyncio
import csv
import io
import json
import logging
import re
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.watchdog.link_checker import is_malformed_host
from app.agents.watchdog.plugin_audit import (
    LATEST_MANUAL,
    LATEST_UNKNOWN,
    LATEST_WPORG,
    _fetch_wporg_version,
    _version_lt,
)
from app.database.engine import AsyncSessionLocal, get_db
from app.database.models import Alert, PluginAudit, Site
from app.security.rate_limit import job_limiter
from app.services.link_repair import (
    prose_from_url,
    recheck_link,
    suggest_replacements,
)
from app.services.performance_rescan import (
    known_urls,
    remeasure_one,
    rescan_ceiling,
    run_bulk_remeasure,
    select_scope,
)
from app.services.performance_rescan import read_progress as read_performance_progress
from app.utils.background import spawn

logger = logging.getLogger(__name__)

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


def alert_query(
    *,
    site_id: str | None = None,
    severity: str | None = None,
    agent: str | None = None,
    status: str | None = None,
    type: str | None = None,
    bucket: str | None = None,
):
    """The one definition of "which alerts match these filters".

    Shared by the list and the CSV export. Two copies would drift, and the
    drift is invisible: an export that quietly filters differently from the
    table it was launched from produces a file someone acts on believing it
    is what they were looking at.
    """
    query = (
        select(Alert, Site.name.label("site_name"))
        .join(Site, Alert.site_id == Site.id)
        .order_by(Alert.created_at.desc())
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
    if bucket and bucket in BUCKET_PREFIXES:
        # Preferred over `type`: one bucket can span several type prefixes
        # (plugins and themes both live under "component"), which a substring
        # match cannot express.
        query = query.where(
            or_(*[Alert.type.startswith(p, autoescape=True) for p in BUCKET_PREFIXES[bucket]])
        )
    if type:
        # autoescape: `_` and `%` are LIKE wildcards, and this value comes
        # straight from the client — `type=_` matched every alert.
        query = query.where(Alert.type.contains(type, autoescape=True))
    return query


@router.get("/alerts", response_model=list[AlertResponse])
async def list_alerts(
    site_id: str | None = None,
    severity: str | None = None,
    agent: str | None = None,
    status: str | None = None,
    type: str | None = None,
    bucket: str | None = None,
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    """Cross-agent alert listing — pass `agent` to scope to one agent (the
    Watchdog page always does); omit it for a site's full alert history."""
    result = await db.execute(
        alert_query(
            site_id=site_id, severity=severity, agent=agent,
            status=status, type=type, bucket=bucket,
        ).limit(limit).offset(offset)
    )
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


# Columns worth a spreadsheet, per finding type. A single generic shape would
# make the useful part — the URL, the version, the score — a blob of JSON in
# one cell, which is the difference between a file someone can work from and
# one they have to re-read by hand.
_EXPORT_COLUMNS: dict[str, list[tuple[str, str]]] = {
    "broken_link": [
        ("url", "Broken URL"),
        ("status_code", "HTTP status"),
        ("malformed", "Malformed href"),
        ("is_internal", "Internal"),
        ("found_on", "Found on"),
        ("found_on_count", "Pages affected"),
    ],
    "performance": [
        ("page_url", "Page"),
        ("speed_score", "PSI score"),
        ("lcp_ms", "LCP (ms)"),
        ("cls", "CLS"),
        ("ttfb_ms", "TTFB (ms)"),
        ("source", "Measured by"),
    ],
    "component": [
        ("plugin_name", "Component"),
        ("component_type", "Kind"),
        ("installed_version", "Installed"),
        ("latest_version", "Latest"),
    ],
}
# Every export carries these, so a file is identifiable without its filename.
_CORE_COLUMNS = [
    ("severity", "Severity"),
    ("type", "Type"),
    ("site_name", "Site"),
    ("title", "Finding"),
    ("description", "Detail"),
    ("status", "Status"),
    ("created_at", "First seen"),
]
# A bounded file rather than a request that ties up a worker indefinitely.
EXPORT_MAX_ROWS = 10_000


def _cell(value: Any) -> str:
    """One value, flattened for a spreadsheet cell."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, list):
        return " | ".join(str(v) for v in value)
    if isinstance(value, dict):
        return json.dumps(value, separators=(",", ":"))
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    return str(value)


@router.get("/alerts/export.csv")
async def export_alerts(
    site_id: str | None = None,
    severity: str | None = None,
    agent: str | None = "watchdog",
    status: str | None = None,
    bucket: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """The current view as CSV.

    Takes the same filters as the list, because it is meant to be the list —
    the table is paginated fifteen rows at a time, so exporting what is on
    screen would hand over fifteen of four hundred findings and look complete.
    """
    rows = (await db.execute(
        alert_query(
            site_id=site_id, severity=severity, agent=agent,
            status=status, bucket=bucket,
        ).limit(EXPORT_MAX_ROWS)
    )).all()

    extra = _EXPORT_COLUMNS.get(bucket or "", [])
    headers = [label for _, label in _CORE_COLUMNS] + [label for _, label in extra]

    def generate() -> Iterator[str]:
        buffer = io.StringIO()
        writer = csv.writer(buffer)

        def flush() -> str:
            buffer.seek(0)
            chunk = buffer.read()
            buffer.seek(0)
            buffer.truncate(0)
            return chunk

        writer.writerow(headers)
        yield flush()

        for alert, site_name in rows:
            meta = alert.metadata_ or {}
            record = {**{c.name: getattr(alert, c.name) for c in alert.__table__.columns},
                      "site_name": site_name}
            writer.writerow(
                [_cell(record.get(key)) for key, _ in _CORE_COLUMNS]
                + [_cell(meta.get(key)) for key, _ in extra]
            )
            yield flush()

    stamp = datetime.now(UTC).strftime("%Y-%m-%d")
    name = f"{bucket or 'watchdog'}-findings-{stamp}.csv"
    return StreamingResponse(
        generate(),
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": f'attachment; filename="{name}"',
            # So the UI can say "10,000 of 12,431" rather than implying the
            # file is everything.
            "X-Row-Count": str(len(rows)),
            "X-Row-Limit": str(EXPORT_MAX_ROWS),
        },
    )


class AlertSummary(BaseModel):
    total: int
    by_type: dict[str, int]        # buckets: broken_link | performance | plugin | other
    by_severity: dict[str, int]
    matrix: dict[str, dict[str, int]]  # bucket -> severity -> count


# Alert types grouped into the buckets the Watchdog tabs show. Declared once
# so the summary counts and the list filter can never drift apart — they did,
# via a substring `type` filter that could not express "plugins AND themes".
BUCKET_PREFIXES: dict[str, tuple[str, ...]] = {
    # Plugins, themes, and the site-level notices about auditing them.
    "component": ("plugin", "theme", "component"),
    "broken_link": ("broken_link",),
    "performance": ("performance",),
}


def _bucket(alert_type: str) -> str:
    for name, prefixes in BUCKET_PREFIXES.items():
        if alert_type.startswith(prefixes):
            return name
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
    from app.agents.watchdog.link_checker import LinkChecker
    from app.agents.watchdog.performance import PerformanceMonitor
    from app.agents.watchdog.plugin_audit import PluginAuditor
    from app.services.job_executor import AGENT_TIMEOUTS
    from app.services.site_scope import select_monitored_sites

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

        await _record_run(db, ran=ran, failures=failures, module=module)
        await db.commit()


async def _record_run(
    db: AsyncSession, *, ran: int, failures: list[str],
    module: str | None = None, finished: bool = True,
) -> None:
    """Persist the re-run outcome so a failed background run is visible.

    Without this the only trace of a failure was a server-side log line, and
    the UI rendered an empty alert list as "Your sites are healthy."

    `finished` matters because this is written twice: once when a run is
    queued, to clear the previous banner, and once when it ends. Both used to
    stamp `finished_at`, so a run that had not started yet was indistinguishable
    from one that had completed — and with agents now startable individually,
    "has mine finished" is a question someone actually asks.
    """
    from app.services.app_settings import set_json_setting

    now = datetime.now(UTC).isoformat()
    await set_json_setting(
        db,
        _RUN_STATUS_KEY,
        {
            "finished_at": now if finished else None,
            "started_at": None if finished else now,
            "running": not finished,
            # Which agent, so a banner can name what is in flight rather than
            # saying "agents" when only one was asked for.
            "module": module,
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
    req: FlushRequest, db: AsyncSession = Depends(get_db)
) -> dict[str, str]:
    """Re-run the watchdog agents against current reality.

    Deliberately non-destructive: the agents reconcile their own alerts, so
    existing rows keep their first-seen time and their acknowledged/dismissed
    state, and a re-run that fails leaves the previous findings on screen
    instead of replacing them with a false all-clear.

    Detached via `spawn` rather than Starlette's BackgroundTasks, which run
    INSIDE the request task: a full crawl takes minutes, and uvicorn's
    graceful shutdown waits for request tasks — so a re-run in flight would
    wedge every restart behind "Waiting for background tasks to complete".
    """
    # Marks a run as in flight; the background task overwrites it on exit.
    await _record_run(db, ran=0, failures=[], module=req.module, finished=False)
    spawn(_run_watchdog(req.site_id, req.module), name="watchdog-rerun")
    return {"status": "running", "message": "Re-running watchdog in background"}


# ── Broken-link repair ────────────────────────────────────────────────────────
#
# Re-check verifies one link now, because the sweep rotates and would otherwise
# take days to confirm a fix. Suggestions apply only to malformed hrefs, where
# the prose someone pasted into the link field is the only clue to what they
# meant — and they are offered, never applied.


class LinkActionRequest(BaseModel):
    site_id: str
    url: str = Field(min_length=1, max_length=4096)


async def _link_is_reported(db: AsyncSession, site_id: str, url: str) -> bool:
    """Whether this site already has a finding naming this URL.

    Both endpoints below take a URL from the client, and one of them makes the
    server fetch it. Restricting them to URLs the site has already reported
    keeps this from becoming a general-purpose fetcher — a public-address
    check alone would still allow any host on the internet.
    """
    rows = (await db.execute(
        select(Alert.metadata_).where(
            Alert.site_id == site_id,
            Alert.agent == "watchdog",
            Alert.type == "broken_link",
        )
    )).scalars().all()
    return any(isinstance(m, dict) and m.get("url") == url for m in rows)


@router.post("/links/recheck", dependencies=[Depends(job_limiter)])
async def recheck_broken_link(
    payload: LinkActionRequest, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """Verify one reported link now, and clear its finding if it works."""
    site = await db.get(Site, payload.site_id)
    if not site:
        raise HTTPException(status_code=404, detail="Site not found")

    url = payload.url.strip()
    if not await _link_is_reported(db, payload.site_id, url):
        raise HTTPException(
            status_code=422, detail="That URL is not a reported link for this site"
        )
    try:
        return await recheck_link(db, payload.site_id, site.url, url)
    except Exception as exc:
        logger.warning("Link re-check failed for %s: %s", url, exc)
        raise HTTPException(status_code=502, detail=f"Could not check the link: {exc}") from exc


@router.get("/links/suggestions")
async def link_suggestions(
    site_id: str,
    url: str = Query(min_length=1, max_length=4096),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Pages the prose in a malformed href probably meant.

    Empty for a link to a real page that happens to fail: the destination is
    known there, and proposing a replacement would be inventing an intention
    nobody expressed.
    """
    if not await _link_is_reported(db, site_id, url):
        raise HTTPException(
            status_code=422, detail="That URL is not a reported link for this site"
        )
    suggestions = await suggest_replacements(db, site_id, url)
    return {
        "url": url,
        "prose": prose_from_url(url) if is_malformed_host(url) else "",
        "malformed": is_malformed_host(url),
        "suggestions": [
            {"url": s.url, "title": s.title, "score": s.score, "matched": s.matched}
            for s in suggestions
        ],
    }


# ── Performance re-measurement ────────────────────────────────────────────────
#
# The scheduled sweep rotates a bounded slice per run, so a page can be days
# from its turn — which means "I just fixed this, show me" had no answer. These
# two endpoints are that answer: one page now, or every page currently
# reported. Both go through the same measurement and reconciliation code the
# agent uses, so a hand-triggered result and a scheduled one cannot disagree.


class PerformanceMeasureRequest(BaseModel):
    site_id: str
    url: str = Field(min_length=1, max_length=2048)


@router.post("/performance/measure", dependencies=[Depends(job_limiter)])
async def measure_performance_page(
    payload: PerformanceMeasureRequest, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """Measure one page now and return what it scored.

    Synchronous on purpose: the caller clicked this to see a number, and a
    single PageSpeed round-trip is seconds, not minutes. The session is
    released before the network call so the wait does not sit on a pooled
    connection.
    """
    site = await db.get(Site, payload.site_id)
    if not site:
        raise HTTPException(status_code=404, detail="Site not found")

    url = payload.url.strip()
    # This endpoint makes the server fetch a URL the client names. Restricting
    # it to URLs the site already tracks keeps it from being a general-purpose
    # fetcher — a public-address check alone would still allow any host.
    if url not in await known_urls(db, site):
        raise HTTPException(
            status_code=422,
            detail="That URL is not a tracked page of this site",
        )

    # Ends the read transaction before minutes of network time.
    await db.rollback()

    try:
        return await remeasure_one(payload.site_id, url)
    except Exception as exc:
        logger.warning("Manual performance measure failed for %s: %s", url, exc)
        raise HTTPException(status_code=502, detail=f"Measurement failed: {exc}") from exc


class PerformanceRescanRequest(BaseModel):
    site_id: str
    # "reported" is what the Performance tab lists; "all" is every tracked
    # page, bounded — see the ceiling in the response.
    scope: Literal["reported", "all"] = "reported"


@router.post("/performance/rescan", dependencies=[Depends(job_limiter)])
async def rescan_performance(
    payload: PerformanceRescanRequest, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """Re-measure a whole scope in the background.

    Returns how many pages were queued AND how many were candidates. A batch
    capped at the ceiling must not read as a complete sweep — that is the
    difference between "every reported page was re-measured" and "the 200
    stalest of 1,836 were".
    """
    site = await db.get(Site, payload.site_id)
    if not site:
        raise HTTPException(status_code=404, detail="Site not found")

    current = await read_performance_progress()
    if current.get("running"):
        raise HTTPException(
            status_code=409,
            detail="A performance re-measure is already running. Wait for it to finish.",
        )

    candidates = await select_scope(db, site, payload.scope)
    if not candidates:
        raise HTTPException(
            status_code=404,
            detail=(
                "No reported pages to re-measure"
                if payload.scope == "reported"
                else "No tracked pages for this site"
            ),
        )

    ceiling = rescan_ceiling()
    queued = candidates[:ceiling]
    spawn(
        run_bulk_remeasure(payload.site_id, queued, payload.scope),
        name=f"perf-rescan-{len(queued)}",
    )
    return {
        "queued": len(queued),
        "candidates": len(candidates),
        "truncated": len(candidates) > len(queued),
        "scope": payload.scope,
    }


@router.get("/performance/rescan/status")
async def performance_rescan_status() -> dict[str, Any]:
    """Progress of the current or most recent performance re-measure."""
    return await read_performance_progress()


# ── Component inventory ───────────────────────────────────────────────────────
#
# Reading /wp/v2/plugins and /wp/v2/themes requires an Application Password.
# Without one a site could not be audited at all, so its components can be
# recorded by hand here and are then checked for updates and known CVEs on
# exactly the same path as WordPress-sourced ones.


class ComponentResponse(BaseModel):
    id: str
    site_id: str
    component_type: str
    slug: str
    name: str | None
    installed_version: str
    latest_version: str
    # "wporg" | "manual" | "unknown" — without this the UI cannot tell
    # "confirmed current" from "never looked up", which read identically.
    latest_source: str
    risk_level: str
    is_active: bool | None
    source: str
    outdated: bool
    vulnerability_count: int
    audited_at: datetime


class ComponentCreate(BaseModel):
    site_id: str
    component_type: Literal["plugin", "theme"]
    # The wp.org / WPScan lookup key — "akismet", not "Akismet Anti-Spam".
    slug: str = Field(min_length=1, max_length=255)
    name: str | None = Field(default=None, max_length=255)
    installed_version: str = Field(min_length=1, max_length=50)
    # For premium and custom components the directory has never heard of —
    # Avada, Swift Performance, anything built in-house — the operator is the
    # only possible authority on what the newest release is.
    latest_version: str | None = Field(default=None, max_length=50)
    # None is meaningful: "I don't know", distinct from "installed, inactive".
    is_active: bool | None = None


class ComponentUpdate(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    installed_version: str | None = Field(default=None, min_length=1, max_length=50)
    latest_version: str | None = Field(default=None, max_length=50)
    is_active: bool | None = None


def _normalize_slug(raw: str) -> str:
    """wp.org slugs are lowercase and hyphenated. Accepting "Akismet" or a
    stray "akismet/akismet.php" and normalising beats a lookup that silently
    finds nothing and reports the component as up to date."""
    slug = raw.strip().lower()
    if "/" in slug:
        slug = slug.split("/")[0]
    return re.sub(r"[^a-z0-9._-]+", "-", slug).strip("-")


def _component_row(a: PluginAudit) -> dict[str, Any]:
    details = a.vulnerability_details or {}
    vulns = details.get("vulnerabilities") if isinstance(details, dict) else None
    return {
        "id": a.id,
        "site_id": a.site_id,
        "component_type": a.component_type,
        "slug": a.plugin_slug,
        "name": a.plugin_name,
        "installed_version": a.installed_version,
        "latest_version": a.latest_version,
        "latest_source": a.latest_source,
        "risk_level": a.risk_level,
        "is_active": a.is_active,
        "source": a.source,
        "outdated": (
            a.latest_source != LATEST_UNKNOWN
            and _version_lt(a.installed_version, a.latest_version)
        ),
        "vulnerability_count": len(vulns) if isinstance(vulns, list) else 0,
        "audited_at": a.audited_at,
    }


async def _lookup_wporg(slug: str, component_type: str) -> str | None:
    """Latest version from the WordPress.org directory, or None if unlisted."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            return await _fetch_wporg_version(client, slug, component_type)
    except Exception:
        return None


class LookupResponse(BaseModel):
    slug: str
    found: bool
    latest_version: str | None


@router.get("/components/lookup", response_model=LookupResponse)
async def lookup_component(
    slug: str = Query(min_length=1, max_length=255),
    component_type: Literal["plugin", "theme"] = "plugin",
) -> dict[str, Any]:
    """Resolve a slug against WordPress.org before anything is saved.

    Lets the form tell the operator immediately whether the directory knows
    this component — and therefore whether they need to supply the latest
    version themselves, as they must for Avada, Swift Performance or an
    in-house build.
    """
    normalized = _normalize_slug(slug)
    if not normalized:
        return {"slug": slug, "found": False, "latest_version": None}
    latest = await _lookup_wporg(normalized, component_type)
    return {"slug": normalized, "found": latest is not None, "latest_version": latest}


@router.get("/components", response_model=list[ComponentResponse])
async def list_components(
    site_id: str | None = None,
    source: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    """Every audited component — WordPress-read and hand-entered alike."""
    query = select(PluginAudit).order_by(
        PluginAudit.component_type, PluginAudit.plugin_slug
    )
    if site_id:
        query = query.where(PluginAudit.site_id == site_id)
    if source:
        query = query.where(PluginAudit.source == source)
    rows = (await db.execute(query)).scalars().all()
    return [_component_row(a) for a in rows]


@router.post("/components", response_model=ComponentResponse, status_code=201)
async def create_component(
    payload: ComponentCreate, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    site = await db.get(Site, payload.site_id)
    if not site:
        raise HTTPException(status_code=404, detail="Site not found")

    slug = _normalize_slug(payload.slug)
    if not slug:
        raise HTTPException(status_code=422, detail="Slug is empty after normalisation")

    existing = (await db.execute(
        select(PluginAudit).where(
            PluginAudit.site_id == payload.site_id,
            PluginAudit.component_type == payload.component_type,
            PluginAudit.plugin_slug == slug,
        )
    )).scalar_one_or_none()
    if existing:
        raise HTTPException(
            status_code=409,
            detail=f"{payload.component_type} '{slug}' is already recorded for this site",
        )

    installed = payload.installed_version.strip()

    # Resolve the latest version NOW rather than leaving it equal to installed
    # until the next 6-hourly audit — which showed every newly-added component
    # as "up to date" no matter how old it was.
    latest, latest_source = installed, LATEST_UNKNOWN
    if payload.latest_version and payload.latest_version.strip():
        latest, latest_source = payload.latest_version.strip(), LATEST_MANUAL
    else:
        found = await _lookup_wporg(slug, payload.component_type)
        if found:
            latest, latest_source = found, LATEST_WPORG

    row = PluginAudit(
        site_id=payload.site_id,
        plugin_slug=slug,
        plugin_name=payload.name or slug,
        component_type=payload.component_type,
        installed_version=installed,
        latest_version=latest,
        latest_source=latest_source,
        risk_level=(
            "high" if latest_source != LATEST_UNKNOWN and _version_lt(installed, latest)
            else "unknown" if latest_source == LATEST_UNKNOWN
            else "low"
        ),
        vulnerability_details={},
        is_active=payload.is_active,
        source="manual",
    )
    db.add(row)
    await db.flush()
    return _component_row(row)


@router.put("/components/{component_id}", response_model=ComponentResponse)
async def update_component(
    component_id: str, payload: ComponentUpdate, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    row = await db.get(PluginAudit, component_id)
    if not row:
        raise HTTPException(status_code=404, detail="Component not found")
    if row.source != "manual":
        # WordPress is the authority for what it reports; editing that here
        # would be overwritten on the next run anyway.
        raise HTTPException(
            status_code=409,
            detail="This component is read from WordPress and cannot be edited by hand",
        )
    # `model_fields_set` distinguishes "not supplied" from an explicit null.
    # It matters for is_active, whose whole point is being tri-state: without
    # it there is no way to move a component back to "not known" once someone
    # has said active or inactive.
    sent = payload.model_fields_set

    if "name" in sent:
        row.plugin_name = (payload.name or "").strip() or row.plugin_slug
    if payload.installed_version is not None:
        row.installed_version = payload.installed_version.strip()
    if "latest_version" in sent:
        supplied = (payload.latest_version or "").strip()
        if supplied:
            # Recorded as `manual` so the auditor preserves it: for a premium
            # or custom component wp.org will never have an answer, and
            # overwriting this with the installed version each run is exactly
            # what made it look permanently current.
            row.latest_version, row.latest_source = supplied, LATEST_MANUAL
        else:
            # Cleared — hand authority back to the directory lookup.
            row.latest_source = LATEST_UNKNOWN
    if "is_active" in sent:
        row.is_active = payload.is_active

    # An unresolved component has no independent "latest": it must follow the
    # installed version, or bumping installed past a stale mirrored value
    # leaves latest reading lower than installed, which is nonsense on screen.
    if row.latest_source == LATEST_UNKNOWN:
        row.latest_version = row.installed_version

    row.risk_level = (
        "unknown" if row.latest_source == LATEST_UNKNOWN
        else "high" if _version_lt(row.installed_version, row.latest_version)
        else "low"
    )
    await db.flush()
    return _component_row(row)


@router.delete("/components/{component_id}", status_code=204)
async def delete_component(component_id: str, db: AsyncSession = Depends(get_db)) -> None:
    row = await db.get(PluginAudit, component_id)
    if not row:
        raise HTTPException(status_code=404, detail="Component not found")
    if row.source != "manual":
        raise HTTPException(
            status_code=409,
            detail="This component is read from WordPress; remove it in WordPress instead",
        )
    await db.delete(row)
