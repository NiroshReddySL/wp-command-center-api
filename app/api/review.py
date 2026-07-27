from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.engine import get_db
from app.database.models import ReviewItem, Site

router = APIRouter()


class ReviewResponse(BaseModel):
    id: str
    alert_id: str | None
    agent: str
    action_type: str
    payload: dict[str, Any]
    status: str
    reviewer_notes: str | None
    site_name: str
    site_id: str
    created_at: datetime
    reviewed_at: datetime | None


class ReviewAction(BaseModel):
    reviewer_notes: str | None = None


@router.get("", response_model=list[ReviewResponse])
async def list_review_items(
    status: str = "pending",
    agent: str | None = None,
    site_id: str | None = None,
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    query = (
        select(ReviewItem, Site.name.label("site_name"))
        .join(Site, ReviewItem.site_id == Site.id)
        .where(ReviewItem.status == status)
        .order_by(ReviewItem.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    if agent:
        query = query.where(ReviewItem.agent == agent)
    if site_id:
        query = query.where(ReviewItem.site_id == site_id)

    result = await db.execute(query)
    rows = result.all()

    return [
        {
            "id": item.id,
            "alert_id": item.alert_id,
            "agent": item.agent,
            "action_type": item.action_type,
            "payload": item.payload,
            "status": item.status,
            "reviewer_notes": item.reviewer_notes,
            "site_name": site_name,
            "site_id": item.site_id,
            "created_at": item.created_at,
            "reviewed_at": item.reviewed_at,
        }
        for item, site_name in rows
    ]


@router.put("/{item_id}/approve", response_model=ReviewResponse)
async def approve_item(
    item_id: str,
    body: ReviewAction,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    result = await db.execute(
        select(ReviewItem, Site.name.label("site_name"))
        .join(Site, ReviewItem.site_id == Site.id)
        .where(ReviewItem.id == item_id)
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail="Review item not found")

    item, site_name = row
    item.status = "approved"
    item.reviewer_notes = body.reviewer_notes
    item.reviewed_at = datetime.now(timezone.utc)
    await db.flush()

    return {
        "id": item.id,
        "alert_id": item.alert_id,
        "agent": item.agent,
        "action_type": item.action_type,
        "payload": item.payload,
        "status": item.status,
        "reviewer_notes": item.reviewer_notes,
        "site_name": site_name,
        "site_id": item.site_id,
        "created_at": item.created_at,
        "reviewed_at": item.reviewed_at,
    }


@router.put("/{item_id}/reject", response_model=ReviewResponse)
async def reject_item(
    item_id: str,
    body: ReviewAction,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    result = await db.execute(
        select(ReviewItem, Site.name.label("site_name"))
        .join(Site, ReviewItem.site_id == Site.id)
        .where(ReviewItem.id == item_id)
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail="Review item not found")

    item, site_name = row
    item.status = "rejected"
    item.reviewer_notes = body.reviewer_notes
    item.reviewed_at = datetime.now(timezone.utc)
    await db.flush()

    return {
        "id": item.id,
        "alert_id": item.alert_id,
        "agent": item.agent,
        "action_type": item.action_type,
        "payload": item.payload,
        "status": item.status,
        "reviewer_notes": item.reviewer_notes,
        "site_name": site_name,
        "site_id": item.site_id,
        "created_at": item.created_at,
        "reviewed_at": item.reviewed_at,
    }
