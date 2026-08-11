"""Site reports — generate, read, export.

Reports are stored snapshots, never recomputed on read. The in-app view and
the HTML export therefore render the identical payload and cannot disagree,
and a report that was sent last month still says what it said when it was
sent.
"""
from datetime import UTC, datetime
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.engine import get_db
from app.database.models import ReviewItem, Site
from app.reports.builder import REPORT_ACTION_TYPE, generate_and_store
from app.reports.html import render_report
from app.reports.period import DEFAULT_RANGE, Period
from app.reports.retention import (
    KEEP_LATEST,
    active,
    apply_retention,
    empty_trash,
    expires_at,
    trashed,
)
from app.security.auth import require_user
from app.security.rate_limit import job_limiter
from app.services.site_scope import select_monitored_sites

router = APIRouter(dependencies=[Depends(require_user)])


class ReportSummary(BaseModel):
    id: str
    site_id: str
    title: str
    generated_at: datetime
    # Which period the report covers, distinct from when it was run. Two
    # reports generated the same afternoon can cover different months, and a
    # list keyed only on generation time cannot tell them apart.
    period_label: str = ""
    period_start: str = ""
    period_end: str = ""
    severity_counts: dict[str, int] = {}
    unavailable_sources: int = 0
    # Lifecycle. `trashed_at` set means it is in the trash; `expires_at` is
    # when the purge will take it, so the UI can say how long is left rather
    # than leaving a deadline implicit.
    locked: bool = False
    trashed_at: datetime | None = None
    expires_at: datetime | None = None


def _payload(item: ReviewItem) -> dict[str, Any]:
    return (item.payload or {}).get("report") or {}


async def _load(db: AsyncSession, report_id: str) -> ReviewItem:
    item = await db.get(ReviewItem, report_id)
    if item is None or item.action_type != REPORT_ACTION_TYPE:
        raise HTTPException(status_code=404, detail="Report not found")
    return item


def _summary(item: ReviewItem) -> dict[str, Any]:
    data = _payload(item)
    return {
        "id": item.id,
        "site_id": item.site_id,
        "title": (item.payload or {}).get("title", "Site Report"),
        "generated_at": item.created_at,
        # Reports predating the period picker have no label stored; fall
        # back to their raw dates rather than showing an empty chip.
        "period_label": data.get("period_label")
        or f"{data.get('period_start', '')} → {data.get('period_end', '')}",
        "period_start": data.get("period_start", ""),
        "period_end": data.get("period_end", ""),
        "severity_counts": data.get("severity_counts", {}),
        "unavailable_sources": sum(
            1 for s in data.get("sources", []) if not s.get("available")
        ),
        "locked": bool(item.locked),
        "trashed_at": item.trashed_at,
        "expires_at": expires_at(item),
    }


@router.get("", response_model=list[ReportSummary])
async def list_reports(
    site_id: str | None = None,
    # Active by default: the trash is somewhere you go, not something mixed
    # into the working list.
    state: Literal["active", "trashed", "all"] = "active",
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    if state == "active":
        query = active(REPORT_ACTION_TYPE, site_id)
    elif state == "trashed":
        query = trashed(REPORT_ACTION_TYPE, site_id)
    else:
        query = select(ReviewItem).where(ReviewItem.action_type == REPORT_ACTION_TYPE)
        if site_id:
            query = query.where(ReviewItem.site_id == site_id)

    items = (await db.execute(
        query.order_by(ReviewItem.created_at.desc()).limit(limit)
    )).scalars().all()
    return [_summary(item) for item in items]


@router.get("/{report_id}")
async def get_report(report_id: str, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """The stored snapshot, exactly as generated."""
    item = await _load(db, report_id)
    return {"id": item.id, "site_id": item.site_id, **_payload(item)}


@router.get("/{report_id}/export.html", response_class=HTMLResponse)
async def export_report(report_id: str, db: AsyncSession = Depends(get_db)) -> HTMLResponse:
    """Standalone HTML — no scripts, no external requests, prints to PDF."""
    item = await _load(db, report_id)
    data = _payload(item)
    if not data:
        raise HTTPException(status_code=409, detail="This report has no stored content")

    # Named for the period it covers, not the day it was exported — the same
    # report downloaded twice must not produce two differently-named files.
    site = (data.get("site_name") or "site").lower().replace(" ", "-")
    span = f"{data.get('period_start', '')}-to-{data.get('period_end', '')}".strip("-")
    filename = f"{site}-report-{span or str(data.get('generated_at', ''))[:10]}.html"
    return HTMLResponse(
        content=render_report(data),
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


class GenerateRequest(BaseModel):
    site_id: str | None = None  # omit to generate for every monitored site
    # The same preset keys every other range control uses, so "last 28 days"
    # in a report and on a dashboard resolve to identical dates.
    range: str = DEFAULT_RANGE
    start_date: str | None = None   # required, with end_date, when range=custom
    end_date: str | None = None


@router.post("", dependencies=[Depends(job_limiter)])
async def generate(
    payload: GenerateRequest, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """Build a report for a period and freeze it.

    Runs inline rather than detached: generation is a handful of aggregate
    queries plus the Search Console and Analytics pulls, so the caller can be
    handed the report it just asked for.
    """
    # Raises 422 with the specific problem — a custom range missing a date is
    # a request error, not something to silently substitute a default for.
    period = Period.from_request(payload.range, payload.start_date, payload.end_date)

    query = select_monitored_sites()
    if payload.site_id:
        query = query.where(Site.id == payload.site_id)
    sites = (await db.execute(query)).scalars().all()
    if not sites:
        raise HTTPException(status_code=404, detail="No monitored sites to report on")

    created = [await generate_and_store(db, site, period) for site in sites]

    # Applied per site, so three sites keep three reports each. Old ones go to
    # the trash rather than being deleted, and the count is returned rather
    # than left for the user to notice something went missing.
    trashed_ids: list[str] = []
    for site in sites:
        trashed_ids += await apply_retention(db, REPORT_ACTION_TYPE, site.id)

    return {
        "generated": len(created),
        "report_ids": [item.id for item in created],
        "site_ids": [item.site_id for item in created],
        "period_start": period.start_iso,
        "period_end": period.end_iso,
        "period_label": period.label,
        "trashed": len(trashed_ids),
        "keep_latest": KEEP_LATEST,
    }


# ── Lifecycle ─────────────────────────────────────────────────────────────────
#
# Deletion is two-stage. A report may already have been sent to someone, so
# neither a retention rule nor a misclick should be able to destroy one
# outright — the first delete moves it to the trash, and only a second one, or
# thirty days, removes it.


class ReportPatch(BaseModel):
    locked: bool


@router.patch("/{report_id}", response_model=ReportSummary)
async def update_report(
    report_id: str, payload: ReportPatch, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """Lock or unlock a report.

    Locking a trashed report restores it: someone protecting a report they
    found in the trash means they want to keep it, and leaving it there to be
    purged would honour the letter of the request and not the point of it.
    """
    item = await _load(db, report_id)
    item.locked = payload.locked
    if payload.locked:
        item.trashed_at = None
    await db.flush()
    return _summary(item)


@router.post("/{report_id}/restore", response_model=ReportSummary)
async def restore_report(
    report_id: str, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """Take a report back out of the trash — and lock it.

    The lock is not an extra: retention would re-trash it on the next generate
    if three newer reports already exist, so restoring without locking would
    look like the button had not worked.
    """
    item = await _load(db, report_id)
    if item.trashed_at is None:
        raise HTTPException(status_code=409, detail="This report is not in the trash")
    item.trashed_at = None
    item.locked = True
    await db.flush()
    return _summary(item)


@router.delete("/trash", status_code=200)
async def empty_report_trash(
    site_id: str | None = None, db: AsyncSession = Depends(get_db)
) -> dict[str, int]:
    """Discard the trash now instead of waiting out the 30 days."""
    return {"deleted": await empty_trash(db, REPORT_ACTION_TYPE, site_id)}


@router.delete("/{report_id}", status_code=200)
async def delete_report(
    report_id: str, db: AsyncSession = Depends(get_db)
) -> dict[str, str]:
    """Move a report to the trash, or — if it is already there — delete it.

    A locked report is refused rather than silently unlocked. The lock exists
    to make deletion require a deliberate second act, and quietly overriding
    it here would remove the only protection it offers.
    """
    item = await _load(db, report_id)
    if item.locked:
        raise HTTPException(
            status_code=409,
            detail="This report is locked. Unlock it before deleting.",
        )
    if item.trashed_at is not None:
        await db.delete(item)
        return {"status": "deleted"}
    item.trashed_at = datetime.now(UTC)
    await db.flush()
    return {"status": "trashed"}
