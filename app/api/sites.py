import logging
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.wordpress import WordPressConnector
from app.database.engine import get_db
from app.database.models import (
    Alert,
    ContentPost,
    PerformanceSnapshot,
    PluginAudit,
    ReviewItem,
    Site,
    SiteConfig,
    Variant,
)
from app.security.auth import require_admin
from app.utils.background import spawn

logger = logging.getLogger(__name__)
router = APIRouter()


class SiteCreate(BaseModel):
    name: str
    url: str
    api_key: str | None = None
    webhook_secret: str | None = None


class SiteUpdate(BaseModel):
    name: str | None = None
    status: str | None = None
    webhook_secret: str | None = None


class SiteResponse(BaseModel):
    id: str
    name: str
    url: str
    status: str
    health_score: int
    last_synced_at: datetime | None
    created_at: datetime
    issues_count: int
    speed_score: int | None
    content_freshness: int | None
    content_count: int

    model_config = {"from_attributes": True}


async def _enrich_site(site: Site, db: AsyncSession) -> dict[str, Any]:
    issues_r = await db.execute(
        select(func.count(Alert.id)).where(Alert.site_id == site.id, Alert.status == "open")
    )
    issues_count = issues_r.scalar_one() or 0

    # Real speed score: average of latest performance snapshots for this site
    snap_r = await db.execute(
        select(PerformanceSnapshot.speed_score)
        .where(PerformanceSnapshot.site_id == site.id)
        .order_by(PerformanceSnapshot.snapshot_at.desc())
        .limit(5)
    )
    snap_scores = [row[0] for row in snap_r.all()]
    speed_score = int(sum(snap_scores) / len(snap_scores)) if snap_scores else None

    # Real content freshness: % of posts updated within last 180 days
    posts_r = await db.execute(
        select(ContentPost.health_score).where(ContentPost.site_id == site.id)
    )
    post_scores = [row[0] for row in posts_r.all()]
    content_freshness = int(sum(post_scores) / len(post_scores)) if post_scores else None

    return {
        "id": site.id,
        "name": site.name,
        "url": site.url,
        "status": site.status,
        "health_score": site.health_score,
        "last_synced_at": site.last_synced_at,
        "created_at": site.created_at,
        "issues_count": issues_count,
        "speed_score": speed_score,
        "content_freshness": content_freshness,
        "content_count": len(post_scores),
    }


@router.get("", response_model=list[SiteResponse])
async def list_sites(db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    """List sites with rollups — 4 fixed queries total, independent of site count."""
    result = await db.execute(select(Site).order_by(Site.created_at))
    sites = result.scalars().all()

    # Open-alert counts per site
    issues_r = await db.execute(
        select(Alert.site_id, func.count(Alert.id))
        .where(Alert.status == "open")
        .group_by(Alert.site_id)
    )
    issues_by_site = dict(issues_r.all())

    # Speed: average of the 5 most recent snapshots per site (window function)
    ranked = (
        select(
            PerformanceSnapshot.site_id,
            PerformanceSnapshot.speed_score,
            func.row_number()
            .over(
                partition_by=PerformanceSnapshot.site_id,
                order_by=PerformanceSnapshot.snapshot_at.desc(),
            )
            .label("rn"),
        )
    ).subquery()
    speed_r = await db.execute(
        select(ranked.c.site_id, func.avg(ranked.c.speed_score))
        .where(ranked.c.rn <= 5)
        .group_by(ranked.c.site_id)
    )
    speed_by_site = {sid: int(avg) for sid, avg in speed_r.all() if avg is not None}

    # Content freshness + post count: one grouped query per site
    fresh_r = await db.execute(
        select(ContentPost.site_id, func.avg(ContentPost.health_score), func.count(ContentPost.id))
        .group_by(ContentPost.site_id)
    )
    fresh_by_site: dict[str, int] = {}
    count_by_site: dict[str, int] = {}
    for sid, avg, count in fresh_r.all():
        if avg is not None:
            fresh_by_site[sid] = int(avg)
        count_by_site[sid] = count

    return [
        {
            "id": s.id,
            "name": s.name,
            "url": s.url,
            "status": s.status,
            "health_score": s.health_score,
            "last_synced_at": s.last_synced_at,
            "created_at": s.created_at,
            "issues_count": issues_by_site.get(s.id, 0),
            "speed_score": speed_by_site.get(s.id),
            "content_freshness": fresh_by_site.get(s.id),
            "content_count": count_by_site.get(s.id, 0),
        }
        for s in sites
    ]


@router.post("", response_model=SiteResponse, status_code=201)
async def create_site(payload: SiteCreate, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    # SSRF guard — the server will fetch this URL, so it must be a public host
    from app.security.url_guard import ensure_public_url
    await ensure_public_url(payload.url)

    existing = await db.execute(select(Site).where(Site.url == payload.url))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="Site with this URL already exists")

    api_key = payload.api_key.strip() if payload.api_key else None

    # Test connectivity via public REST API (works with or without auth)
    wp = WordPressConnector(payload.url, api_key)
    try:
        await wp.get_posts(per_page=1)
        status = "active"
    except Exception as exc:
        # Only hard-fail if an API key was provided (auth error = config problem)
        status = "error" if api_key else "active"
        if api_key:
            raise HTTPException(status_code=400, detail=f"Failed to connect to WordPress: {exc}") from exc
    finally:
        await wp.close()

    site = Site(
        name=payload.name,
        url=payload.url,
        api_key=api_key,
        status=status,
        webhook_secret=payload.webhook_secret or None,
    )
    db.add(site)
    await db.flush()
    await db.refresh(site)

    if status == "active":
        try:
            await _do_sync(site, db)
        except Exception:
            pass  # sync failure doesn't block site creation

    return await _enrich_site(site, db)


@router.get("/{site_id}", response_model=SiteResponse)
async def get_site(site_id: str, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    result = await db.execute(select(Site).where(Site.id == site_id))
    site = result.scalar_one_or_none()
    if not site:
        raise HTTPException(status_code=404, detail="Site not found")
    return await _enrich_site(site, db)


@router.put("/{site_id}", response_model=SiteResponse)
async def update_site(site_id: str, payload: SiteUpdate, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    result = await db.execute(select(Site).where(Site.id == site_id))
    site = result.scalar_one_or_none()
    if not site:
        raise HTTPException(status_code=404, detail="Site not found")

    if payload.name:
        site.name = payload.name
    if payload.status:
        site.status = payload.status
    if payload.webhook_secret is not None:
        site.webhook_secret = payload.webhook_secret or None

    await db.flush()
    return await _enrich_site(site, db)


class SiteConfigPayload(BaseModel):
    ga_property_id: str | None = None
    gsc_site_url: str | None = None


class SiteConfigResponse(BaseModel):
    site_id: str
    ga_property_id: str | None
    gsc_site_url: str | None


@router.get("/{site_id}/config", response_model=SiteConfigResponse)
async def get_site_config(site_id: str, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    result = await db.execute(select(SiteConfig).where(SiteConfig.site_id == site_id))
    cfg = result.scalar_one_or_none()
    return {
        "site_id": site_id,
        "ga_property_id": cfg.ga_property_id if cfg else None,
        "gsc_site_url": cfg.gsc_site_url if cfg else None,
    }


@router.put("/{site_id}/config", response_model=SiteConfigResponse)
async def update_site_config(
    site_id: str, payload: SiteConfigPayload, db: AsyncSession = Depends(get_db)
) -> dict[str, Any]:
    result = await db.execute(select(Site).where(Site.id == site_id))
    if not result.scalar_one_or_none():
        raise HTTPException(status_code=404, detail="Site not found")

    cfg_result = await db.execute(select(SiteConfig).where(SiteConfig.site_id == site_id))
    cfg = cfg_result.scalar_one_or_none()

    if cfg:
        if payload.ga_property_id is not None:
            cfg.ga_property_id = payload.ga_property_id or None
        if payload.gsc_site_url is not None:
            cfg.gsc_site_url = payload.gsc_site_url or None
    else:
        cfg = SiteConfig(
            site_id=site_id,
            ga_property_id=payload.ga_property_id or None,
            gsc_site_url=payload.gsc_site_url or None,
        )
        db.add(cfg)

    await db.flush()
    return {"site_id": site_id, "ga_property_id": cfg.ga_property_id, "gsc_site_url": cfg.gsc_site_url}


@router.delete("/{site_id}", status_code=204, dependencies=[Depends(require_admin)])
async def delete_site(site_id: str, db: AsyncSession = Depends(get_db)) -> None:
    result = await db.execute(select(Site).where(Site.id == site_id))
    site = result.scalar_one_or_none()
    if not site:
        raise HTTPException(status_code=404, detail="Site not found")
    await db.delete(site)


@router.post("/{site_id}/sync")
async def sync_site(
    site_id: str,
    flush: bool = False,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    result = await db.execute(select(Site).where(Site.id == site_id))
    site = result.scalar_one_or_none()
    if not site:
        raise HTTPException(status_code=404, detail="Site not found")

    if flush:
        await _flush_site_data(site_id, db)

    # Manual sync must always see fresh WordPress content
    from app.agents.wp_fetch import invalidate as invalidate_wp_cache
    invalidate_wp_cache(site.url)

    synced = await _do_sync(site, db)

    # Commit BEFORE launching the background task — it opens its own session
    # and would otherwise race against this request's uncommitted data.
    await db.commit()

    # Run agents in the background (non-blocking). spawn() holds the
    # reference so it can't be collected mid-run.
    spawn(_run_agents_background(site_id), name=f"sync-agents-{site_id}")

    return {
        "status": "ok",
        "site_id": site_id,
        "posts_synced": synced["posts_synced"],
        "pages_synced": synced["pages_synced"],
        "removed": synced["removed"],
        "mode": synced["mode"],
        "last_synced_at": site.last_synced_at.isoformat() if site.last_synced_at else None,
        "flushed": flush,
    }


async def _flush_site_data(site_id: str, db: AsyncSession) -> None:
    """Delete all agent-generated data for a site, keeping the site record itself."""
    from sqlalchemy import delete as sa_delete

    # Delete variants first (FK → content_posts)
    post_ids_r = await db.execute(select(ContentPost.id).where(ContentPost.site_id == site_id))
    post_ids = [row[0] for row in post_ids_r.all()]
    if post_ids:
        await db.execute(sa_delete(Variant).where(Variant.content_post_id.in_(post_ids)))

    await db.execute(sa_delete(ReviewItem).where(ReviewItem.site_id == site_id))
    await db.execute(sa_delete(Alert).where(Alert.site_id == site_id))
    await db.execute(sa_delete(ContentPost).where(ContentPost.site_id == site_id))
    await db.execute(sa_delete(PerformanceSnapshot).where(PerformanceSnapshot.site_id == site_id))
    await db.execute(sa_delete(PluginAudit).where(PluginAudit.site_id == site_id))

    # Reset site health score
    result = await db.execute(select(Site).where(Site.id == site_id))
    site = result.scalar_one_or_none()
    if site:
        site.health_score = 100
        site.last_synced_at = None
        # content_posts was just wiped — without resetting these, the next
        # sync would think it's already caught up and only pull the tiny
        # incremental delta, leaving almost everything unsynced.
        site.posts_synced_through = None
        site.pages_synced_through = None
        site.last_full_reconciled_at = None

    await db.flush()
    logger.info("Flushed all data for site %s", site_id)


@router.get("/{site_id}/performance")
async def get_site_performance(site_id: str, db: AsyncSession = Depends(get_db)) -> list[dict[str, Any]]:
    result = await db.execute(
        select(PerformanceSnapshot)
        .where(PerformanceSnapshot.site_id == site_id)
        .order_by(PerformanceSnapshot.snapshot_at.desc())
        .limit(50)
    )
    snaps = result.scalars().all()
    return [
        {
            "id": s.id,
            "page_url": s.page_url,
            "lcp": s.lcp,
            "cls": s.cls,
            "fid": s.fid,
            "ttfb": getattr(s, "ttfb", 0.0),
            "speed_score": s.speed_score,
            "strategy": getattr(s, "strategy", "desktop"),
            "snapshot_at": s.snapshot_at,
        }
        for s in snaps
    ]


@router.get("/{site_id}/content")
async def get_site_content(
    site_id: str,
    sort: str = "health_score",
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    order_col = {
        "health_score": ContentPost.health_score,
        "traffic": ContentPost.traffic_30d,
        "title": ContentPost.title,
    }.get(sort, ContentPost.health_score)

    result = await db.execute(
        select(ContentPost)
        .where(ContentPost.site_id == site_id)
        .order_by(order_col)
        .limit(100)
    )
    posts = result.scalars().all()
    return [
        {
            "id": p.id,
            "title": p.title,
            "url": p.url,
            "health_score": p.health_score,
            "traffic_30d": p.traffic_30d,
            "issues": p.issues,
            "last_analyzed_at": p.last_analyzed_at,
        }
        for p in posts
    ]


async def _run_agents_background(site_id: str) -> None:
    """Run all agents for a site with a fresh DB session after sync completes."""
    from app.agents.optimizer.site_context_analyzer import analyze_site_context
    from app.api.agents import run_agents_for_site
    from app.database.engine import AsyncSessionLocal

    try:
        # Run site context analysis first so agents can use it
        async with AsyncSessionLocal() as ctx_db:
            await analyze_site_context(site_id, ctx_db)
            await ctx_db.commit()

        await run_agents_for_site(site_id)
        logger.info("Background agents completed for site %s", site_id)
    except Exception as exc:
        logger.error("Background agents failed for site %s: %s", site_id, exc)


async def _do_sync(site: Site, db: AsyncSession) -> dict[str, Any]:
    """Incrementally mirror WordPress posts + pages into content_posts.

    Delegates to the content_sync service — see its module docstring for the
    incremental/full-reconciliation strategy. Converts connection failures to
    the 502 this endpoint has always returned on sync failure.
    """
    from app.services.content_sync import sync_site_content

    try:
        return await sync_site_content(site, db)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"WordPress connection failed: {exc}") from exc


@router.post("/{site_id}/analyze-context")
async def analyze_site_context_endpoint(
    site_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Re-run site context analysis on demand and return the updated context."""
    result = await db.execute(select(Site).where(Site.id == site_id))
    site = result.scalar_one_or_none()
    if not site:
        raise HTTPException(status_code=404, detail="Site not found")

    from app.agents.optimizer.site_context_analyzer import analyze_site_context
    context = await analyze_site_context(site_id, db)
    if not context:
        raise HTTPException(status_code=422, detail="Could not analyze site context — ensure posts are synced first")
    return context


@router.get("/{site_id}/context")
async def get_site_context(
    site_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Return the stored site context (empty dict if not yet analyzed)."""
    result = await db.execute(select(Site).where(Site.id == site_id))
    site = result.scalar_one_or_none()
    if not site:
        raise HTTPException(status_code=404, detail="Site not found")
    return {
        "site_id": site_id,
        "context": site.site_context or {},
        "analyzed_at": site.site_context_analyzed_at.isoformat() if site.site_context_analyzed_at else None,
    }
