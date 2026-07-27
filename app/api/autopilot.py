import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.engine import get_db
from app.database.models import ContentPost, ReviewItem, Site, Variant
from app.security.rate_limit import job_limiter

logger = logging.getLogger(__name__)
router = APIRouter()


class ChannelVariant(BaseModel):
    variant_id: str
    channel: str
    status: str
    content_preview: str
    content: str


class RepurposePostResponse(BaseModel):
    id: str
    title: str
    site_name: str
    channels: list[ChannelVariant]
    status: str


class ReportResponse(BaseModel):
    id: str
    title: str
    type: str
    generated_at: datetime
    narrative: str | None = None


class ABTestResponse(BaseModel):
    id: str
    page: str
    site_name: str
    variants_count: int
    traffic_split: int
    current_winner: str | None
    confidence: float
    days_running: int
    status: str


@router.get("/repurposer", response_model=list[RepurposePostResponse])
async def get_repurposer(db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    result = await db.execute(
        select(ContentPost, Site.name.label("site_name"))
        .join(Site, ContentPost.site_id == Site.id)
        .order_by(ContentPost.health_score.desc())
        .limit(20)
    )
    rows = result.all()

    # One query for all variants instead of one per post
    post_ids = [post.id for post, _ in rows]
    variants_by_post: dict[str, list[Variant]] = {}
    if post_ids:
        variants_result = await db.execute(
            select(Variant).where(Variant.content_post_id.in_(post_ids))
        )
        for v in variants_result.scalars().all():
            variants_by_post.setdefault(v.content_post_id, []).append(v)

    posts = []
    for post, site_name in rows:
        variants = variants_by_post.get(post.id, [])
        if not variants:
            continue  # Only show posts that have been repurposed

        channels = [
            {
                "variant_id": v.id,
                "channel": v.channel,
                "status": v.status,
                "content_preview": v.content[:120] + "…" if len(v.content) > 120 else v.content,
                "content": v.content,
            }
            for v in variants
        ]

        all_approved = all(c["status"] == "approved" for c in channels)
        any_published = any(c["status"] == "published" for c in channels)
        overall = "published" if any_published else ("approved" if all_approved else "pending")

        posts.append({
            "id": post.id,
            "title": post.title,
            "site_name": site_name,
            "channels": channels,
            "status": overall,
        })

    return posts


@router.get("/reports", response_model=list[ReportResponse])
async def get_reports(
    site_id: str | None = None, db: AsyncSession = Depends(get_db)
) -> list[dict[str, Any]]:
    query = (
        select(ReviewItem)
        .where(ReviewItem.agent == "autopilot", ReviewItem.action_type == "weekly_report")
        .order_by(ReviewItem.created_at.desc())
        .limit(20)
    )
    if site_id:
        query = query.where(ReviewItem.site_id == site_id)
    result = await db.execute(query)
    items = result.scalars().all()

    return [
        {
            "id": item.id,
            "title": (item.payload or {}).get("title", "Weekly Report"),
            "type": (item.payload or {}).get("type", "weekly"),
            "generated_at": item.created_at,
            "narrative": (item.payload or {}).get("narrative"),
        }
        for item in items
    ]


class GenerateReportsRequest(BaseModel):
    site_id: str | None = None  # omit to generate for every active site


@router.post("/reports/generate", dependencies=[Depends(job_limiter)])
async def generate_reports(
    payload: GenerateReportsRequest, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    """On-demand report run — same Reporter agent the Friday schedule uses."""
    from app.agents.autopilot.reporter import Reporter

    query = select(Site).where(Site.status == "active")
    if payload.site_id:
        query = query.where(Site.id == payload.site_id)
    sites = (await db.execute(query)).scalars().all()
    if not sites:
        raise HTTPException(status_code=404, detail="No active sites to report on.")

    generated, failed = 0, []
    for site in sites:
        try:
            await asyncio.wait_for(Reporter(db).run(site.id), timeout=120)
            generated += 1
        except Exception as exc:
            logger.warning("On-demand report failed for site %s: %s", site.id, exc)
            failed.append(site.name)
    if not generated:
        raise HTTPException(status_code=502, detail=f"Report generation failed for: {', '.join(failed)}")
    return {"generated": generated, "failed": failed}


@router.put("/variants/{variant_id}/approve")
async def approve_variant(variant_id: str, db: AsyncSession = Depends(get_db)) -> dict[str, str]:
    result = await db.execute(select(Variant).where(Variant.id == variant_id))
    variant = result.scalar_one_or_none()
    if not variant:
        raise HTTPException(status_code=404, detail="Variant not found")
    variant.status = "approved"

    # Also approve the linked ReviewItem if it exists
    ri_result = await db.execute(
        select(ReviewItem).where(
            ReviewItem.agent == "autopilot",
            ReviewItem.status == "pending",
            ReviewItem.payload["variant_id"].astext == variant_id,
        )
    )
    review_item = ri_result.scalar_one_or_none()
    if review_item:
        review_item.status = "approved"
        review_item.reviewed_at = datetime.now(timezone.utc)

    await db.flush()
    return {"status": "approved", "variant_id": variant_id}


@router.get("/ab-tests", response_model=list[ABTestResponse])
async def get_ab_tests(db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    result = await db.execute(
        select(ReviewItem, Site.name.label("site_name"))
        .join(Site, ReviewItem.site_id == Site.id)
        .where(ReviewItem.agent == "autopilot", ReviewItem.action_type == "ab_test")
        .limit(20)
    )
    rows = result.all()

    return [
        {
            "id": item.id,
            "page": (item.payload or {}).get("page", ""),
            "site_name": site_name,
            "variants_count": (item.payload or {}).get("variants_count", 2),
            "traffic_split": (item.payload or {}).get("traffic_split", 50),
            "current_winner": (item.payload or {}).get("current_winner"),
            "confidence": (item.payload or {}).get("confidence", 0.0),
            "days_running": (item.payload or {}).get("days_running", 0),
            "status": (item.payload or {}).get("status", "running"),
        }
        for item, site_name in rows
    ]
