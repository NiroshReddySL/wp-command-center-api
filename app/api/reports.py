"""Site reports — generate, read, export.

Reports are stored snapshots, never recomputed on read. The in-app view and
the HTML export therefore render the identical payload and cannot disagree,
and a report that was sent last month still says what it said when it was
sent.
"""
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.engine import get_db
from app.database.models import ReviewItem, Site
from app.reports.builder import REPORT_ACTION_TYPE, generate_and_store
from app.reports.html import render_report
from app.security.auth import require_user
from app.security.rate_limit import job_limiter
from app.services.site_scope import select_monitored_sites

router = APIRouter(dependencies=[Depends(require_user)])


class ReportSummary(BaseModel):
    id: str
    site_id: str
    title: str
    generated_at: datetime
    severity_counts: dict[str, int] = {}
    unavailable_sources: int = 0


def _payload(item: ReviewItem) -> dict[str, Any]:
    return (item.payload or {}).get("report") or {}


async def _load(db: AsyncSession, report_id: str) -> ReviewItem:
    item = await db.get(ReviewItem, report_id)
    if item is None or item.action_type != REPORT_ACTION_TYPE:
        raise HTTPException(status_code=404, detail="Report not found")
    return item


@router.get("", response_model=list[ReportSummary])
async def list_reports(
    site_id: str | None = None,
    limit: int = Query(20, ge=1, le=100),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    query = (
        select(ReviewItem)
        .where(ReviewItem.action_type == REPORT_ACTION_TYPE)
        .order_by(ReviewItem.created_at.desc())
        .limit(limit)
    )
    if site_id:
        query = query.where(ReviewItem.site_id == site_id)
    items = (await db.execute(query)).scalars().all()

    return [
        {
            "id": item.id,
            "site_id": item.site_id,
            "title": (item.payload or {}).get("title", "Site Report"),
            "generated_at": item.created_at,
            "severity_counts": _payload(item).get("severity_counts", {}),
            "unavailable_sources": sum(
                1 for s in _payload(item).get("sources", []) if not s.get("available")
            ),
        }
        for item in items
    ]


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

    site = (data.get("site_name") or "site").lower().replace(" ", "-")
    filename = f"{site}-report-{str(data.get('generated_at', ''))[:10]}.html"
    return HTMLResponse(
        content=render_report(data),
        headers={"Content-Disposition": f'inline; filename="{filename}"'},
    )


class GenerateRequest(BaseModel):
    site_id: str | None = None  # omit to generate for every monitored site


@router.post("", dependencies=[Depends(job_limiter)])
async def generate(
    payload: GenerateRequest, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """Build a report from current data and freeze it.

    Runs inline rather than detached: generation is a handful of aggregate
    queries with no external calls, so it finishes in well under a second and
    the caller can be handed the report it just asked for.
    """
    query = select_monitored_sites()
    if payload.site_id:
        query = query.where(Site.id == payload.site_id)
    sites = (await db.execute(query)).scalars().all()
    if not sites:
        raise HTTPException(status_code=404, detail="No monitored sites to report on")

    created = [await generate_and_store(db, site) for site in sites]
    return {
        "generated": len(created),
        "report_ids": [item.id for item in created],
        "site_ids": [item.site_id for item in created],
    }
