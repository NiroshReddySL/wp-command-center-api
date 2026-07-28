import asyncio
from datetime import datetime
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import ColumnElement, Float, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.engine import get_db
from app.database.models import Alert, ContentPost, Site
from app.security.rate_limit import ai_limiter, job_limiter

router = APIRouter()


class SeoOpportunityResponse(BaseModel):
    id: str
    site_id: str
    page_title: str
    page_url: str
    word_count: int | None
    ai_recommendation: str
    site_name: str
    severity: str
    alert_type: str
    metadata: dict


def _slug_from_url(url: str) -> str | None:
    """Last path segment of the WP post URL — the human-readable slug."""
    from urllib.parse import urlparse
    path = urlparse(url or "").path.strip("/")
    return path.rsplit("/", 1)[-1] if path else None


class ContentHealthResponse(BaseModel):
    id: str
    slug: str
    title: str
    url: str
    site_name: str
    content_type: str
    health_score: int
    traffic_30d: int
    traffic_trend: list[int]
    last_analyzed_at: datetime | None
    issues: list[str]
    word_count: int
    reading_time_minutes: int
    score_breakdown: dict
    ai_recommendation: str | None


class ContentHealthListResponse(BaseModel):
    items: list[ContentHealthResponse]
    total: int


class ContentHealthDetailResponse(BaseModel):
    id: str
    slug: str
    title: str
    url: str
    site_id: str
    site_name: str
    content_type: str
    health_score: int
    traffic_30d: int
    traffic_trend: list[int]
    last_analyzed_at: datetime | None
    issues: list[str]
    word_count: int
    reading_time_minutes: int
    score_breakdown: dict
    ai_recommendation: str | None


class InternalLinkResponse(BaseModel):
    id: str
    source_title: str
    source_url: str
    target_title: str
    target_url: str
    anchor_text: str
    anchor_source: str = "title"  # "search_query" when backed by real GSC data, else "title"
    anchor_impressions: int | None = None
    anchor_clicks: int | None = None
    shared_keywords: list[str]
    relevance_score: float | None = None


@router.get("/seo-opportunities", response_model=list[SeoOpportunityResponse])
async def get_seo_opportunities(
    site_id: str | None = None,
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    query = (
        select(Alert, Site.name.label("site_name"))
        .join(Site, Alert.site_id == Site.id)
        .where(
            Alert.agent == "optimizer",
            Alert.type.like("seo_%"),
            Alert.status == "open",
        )
        .order_by(Alert.severity.asc(), Alert.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    if site_id:
        query = query.where(Alert.site_id == site_id)

    result = await db.execute(query)
    rows = result.all()

    return [
        {
            "id": alert.id,
            "site_id": alert.site_id,
            "page_title": (alert.metadata_ or {}).get("page_title", alert.title),
            "page_url": (alert.metadata_ or {}).get("page_url", ""),
            "word_count": (alert.metadata_ or {}).get("word_count"),
            "ai_recommendation": alert.description,
            "site_name": site_name,
            "severity": alert.severity,
            "alert_type": alert.type,
            "metadata": alert.metadata_ or {},
        }
        for alert, site_name in rows
    ]


# Sort fields the UI exposes — column + a sensible "primary" direction so a
# freshly-chosen field reads naturally (e.g. switching to "Last Analyzed"
# without an explicit direction shows the stalest content first, not the
# just-analyzed content, which is what a content team actually wants to see).
_SORT_COLUMNS: dict[str, Any] = {
    "health_score": ContentPost.health_score,
    "traffic_30d": ContentPost.traffic_30d,
    "word_count": ContentPost.word_count,
    "last_analyzed_at": ContentPost.last_analyzed_at,
    "title": ContentPost.title,
}
_DEFAULT_SORT_DIR: dict[str, str] = {
    "health_score": "asc",       # worst-first — what needs attention
    "traffic_30d": "desc",       # highest-traffic first — what matters most
    "word_count": "asc",         # thinnest content first — what needs attention
    "last_analyzed_at": "asc",   # stalest (or never-analyzed) first
    "title": "asc",              # A → Z
}
_CONTENT_TYPES = frozenset({"post", "page"})
_HEALTH_STATUSES = frozenset({"healthy", "needs_work", "poor"})
_ANALYZED_STATES = frozenset({"analyzed", "never"})

# Specific, filterable issue categories — each maps to a stable field already
# computed by ContentScorer's per-category analysis (score_breakdown), never
# to the free-text `issues` display strings (those interpolate live numbers
# like word counts, so no two posts' text is reliably identical — useless as
# a filter key). A never-analyzed post has an empty score_breakdown, so every
# JSONB lookup below resolves to SQL NULL and correctly excludes it (it has
# no *known* issue, not a confirmed clean bill of health).
_ISSUE_CATEGORIES: dict[str, str] = {
    "thin_content": "Thin Content",
    "missing_images": "Missing Images",
    "missing_links": "Missing Links",
    "stale_content": "Stale Content",
    "title_length": "Title Length",
    "heading_structure": "Heading Structure",
    "missing_meta_description": "Missing Meta Description",
    "missing_faq_schema": "Missing FAQ Schema",
}


def _health_status_bounds(status: str) -> tuple[int | None, int | None]:
    """(min_inclusive, max_exclusive) health_score bounds for a status
    bucket — kept in sync with the Healthy/Needs work/Poor badge shown on
    the post detail page, so a filter and its own badge can never disagree."""
    if status == "healthy":
        return 70, None
    if status == "needs_work":
        return 40, 70
    return None, 40  # poor


def _health_status_condition(status: str) -> ColumnElement[bool]:
    lo, hi = _health_status_bounds(status)
    conditions = []
    if lo is not None:
        conditions.append(ContentPost.health_score >= lo)
    if hi is not None:
        conditions.append(ContentPost.health_score < hi)
    return conditions[0] if len(conditions) == 1 else conditions[0] & conditions[1]


def _issue_filter_condition(category: str) -> ColumnElement[bool]:
    """SQL condition for one specific-issue category, read straight out of
    score_breakdown rather than the free-text issues list. "warning"/
    "critical" both count as a problem for the 0-100-scored categories;
    headings' "info" status is deliberately excluded — it means "no
    headings, but the content is short enough that none are expected",
    which ContentScorer itself does not treat as an issue."""
    sb = ContentPost.score_breakdown
    if category == "thin_content":
        return sb["word_count"]["status"].astext != "good"
    if category == "missing_images":
        return sb["images"]["status"].astext != "good"
    if category == "missing_links":
        return sb["links"]["status"].astext != "good"
    if category == "stale_content":
        return sb["freshness"]["status"].astext != "good"
    if category == "title_length":
        return sb["title"]["status"].astext != "good"
    if category == "heading_structure":
        return sb["headings"]["status"].astext == "warning"
    if category == "missing_meta_description":
        return sb["meta_description"]["status"].astext != "good"
    return sb["schema_markup"]["faq_recommendation"].astext == "missing"  # missing_faq_schema


def _serialize_content_post(post: ContentPost, site_name: str) -> dict[str, Any]:
    return {
        "id": post.id,
        "slug": _slug_from_url(post.url) or post.id,
        "title": post.title,
        "url": post.url,
        "site_name": site_name,
        "content_type": post.content_type,
        "health_score": post.health_score,
        "traffic_30d": post.traffic_30d,
        "traffic_trend": post.traffic_trend or [],
        "last_analyzed_at": post.last_analyzed_at,
        "issues": post.issues or [],
        "word_count": post.word_count or 0,
        "reading_time_minutes": post.reading_time_minutes or 0,
        "score_breakdown": post.score_breakdown or {},
        "ai_recommendation": post.ai_recommendation,
    }


@router.get("/content-health", response_model=ContentHealthListResponse)
async def get_content_health(
    sort_by: str = "health_score",
    sort_dir: str | None = None,
    site_id: str | None = None,
    search: str | None = None,
    content_type: str | None = None,
    health_status: list[str] = Query(default=[]),
    has_issues: bool | None = None,
    issue_categories: list[str] = Query(default=[]),
    analyzed: str | None = None,
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Server-side paginated + searched — an enterprise site's content_posts
    table can hold thousands of rows, so both the page window AND the total
    count must come from SQL, never from a capped list the client re-slices.

    Filters combine with AND across dimensions, OR within a multi-select
    dimension: e.g. content_type=page AND health_status IN (poor, needs_work)
    AND issue_categories IN (thin_content, missing_images) finds pages that
    are poor OR needs-work, AND have thin content OR missing images.
    """
    if sort_by not in _SORT_COLUMNS:
        raise HTTPException(status_code=422, detail=f"sort_by must be one of {sorted(_SORT_COLUMNS)}")
    if sort_dir is not None and sort_dir not in ("asc", "desc"):
        raise HTTPException(status_code=422, detail="sort_dir must be 'asc' or 'desc'")
    if content_type is not None and content_type not in _CONTENT_TYPES:
        raise HTTPException(status_code=422, detail=f"content_type must be one of {sorted(_CONTENT_TYPES)}")
    if any(hs not in _HEALTH_STATUSES for hs in health_status):
        raise HTTPException(status_code=422, detail=f"health_status must be one of {sorted(_HEALTH_STATUSES)}")
    if any(cat not in _ISSUE_CATEGORIES for cat in issue_categories):
        raise HTTPException(status_code=422, detail=f"issue_categories must be one of {sorted(_ISSUE_CATEGORIES)}")
    if analyzed is not None and analyzed not in _ANALYZED_STATES:
        raise HTTPException(status_code=422, detail=f"analyzed must be one of {sorted(_ANALYZED_STATES)}")

    base = select(ContentPost, Site.name.label("site_name")).join(Site, ContentPost.site_id == Site.id)
    if site_id:
        base = base.where(ContentPost.site_id == site_id)
    if search:
        term = f"%{search}%"
        base = base.where(ContentPost.title.ilike(term) | ContentPost.url.ilike(term))
    if content_type:
        base = base.where(ContentPost.content_type == content_type)
    if health_status:
        base = base.where(or_(*[_health_status_condition(hs) for hs in health_status]))
    if has_issues is not None:
        base = base.where(
            func.json_array_length(ContentPost.issues) > 0 if has_issues
            else func.json_array_length(ContentPost.issues) == 0
        )
    if issue_categories:
        base = base.where(or_(*[_issue_filter_condition(cat) for cat in issue_categories]))
    if analyzed == "analyzed":
        base = base.where(ContentPost.last_analyzed_at.isnot(None))
    elif analyzed == "never":
        base = base.where(ContentPost.last_analyzed_at.is_(None))

    total = (await db.execute(select(func.count()).select_from(base.subquery()))).scalar_one()

    order_col = _SORT_COLUMNS[sort_by]
    direction = sort_dir or _DEFAULT_SORT_DIR[sort_by]
    order_expr = order_col.asc() if direction == "asc" else order_col.desc()
    if sort_by == "last_analyzed_at":
        # Nullable — always push "never analyzed" to the end regardless of
        # direction, so it can't dominate an ascending (stalest-first) sort.
        order_expr = order_expr.nulls_last()
    query = base.order_by(order_expr).limit(limit).offset(offset)
    result = await db.execute(query)
    rows = result.all()

    items = [_serialize_content_post(post, site_name) for post, site_name in rows]
    return {"items": items, "total": total}


@router.get("/content-health/{ref}", response_model=ContentHealthDetailResponse)
async def get_content_post_detail(
    ref: str,
    site_id: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Fetch one post by UUID or by its WP slug (clean URLs).

    Slug lookups can collide across sites — pass site_id to disambiguate;
    otherwise the most recently analyzed match wins.
    """
    base = select(ContentPost, Site.name.label("site_name")).join(
        Site, ContentPost.site_id == Site.id
    )

    result = await db.execute(base.where(ContentPost.id == ref))
    row = result.first()

    if not row:
        # Slug lookup: post URL path ends in /{ref} or /{ref}/
        slug_q = base.where(
            ContentPost.url.ilike(f"%/{ref}") | ContentPost.url.ilike(f"%/{ref}/")
        ).order_by(ContentPost.last_analyzed_at.desc().nulls_last())
        if site_id:
            slug_q = slug_q.where(ContentPost.site_id == site_id)
        row = (await db.execute(slug_q)).first()

    if not row:
        raise HTTPException(status_code=404, detail="Post not found")
    post, site_name = row
    return {
        "id": post.id,
        "slug": _slug_from_url(post.url) or post.id,
        "title": post.title,
        "url": post.url,
        "site_id": post.site_id,
        "site_name": site_name,
        "content_type": post.content_type,
        "health_score": post.health_score,
        "traffic_30d": post.traffic_30d,
        "traffic_trend": post.traffic_trend or [],
        "last_analyzed_at": post.last_analyzed_at,
        "issues": post.issues or [],
        "word_count": post.word_count or 0,
        "reading_time_minutes": post.reading_time_minutes or 0,
        "score_breakdown": post.score_breakdown or {},
        "ai_recommendation": post.ai_recommendation,
    }


@router.post("/content-health/{post_id}/rescan", dependencies=[Depends(ai_limiter)])
async def rescan_content_post(
    post_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Re-fetch from WordPress and re-run deep analysis for a single post."""
    from datetime import timezone
    result = await db.execute(
        select(ContentPost, Site)
        .join(Site, ContentPost.site_id == Site.id)
        .where(ContentPost.id == post_id)
    )
    row = result.first()
    if not row:
        raise HTTPException(status_code=404, detail="Post not found")
    post, site = row

    if not post.wp_post_id:
        raise HTTPException(status_code=422, detail="Post has no WordPress ID — cannot rescan")

    from app.connectors.wordpress import WordPressConnector
    from app.agents.optimizer.content_scorer import _analyze, _generate_ai_recommendation, _fetch_page_signals
    import re as _re

    wp = WordPressConnector(site.url, site.api_key)
    try:
        wp_post = await wp.get_post(post.wp_post_id, content_type=post.content_type)
    except Exception as exc:
        raise HTTPException(status_code=502, detail=f"WordPress connection failed: {exc}") from exc
    finally:
        await wp.close()

    if not wp_post:
        # A definitive 404 from WordPress proves this post/page no longer
        # exists there (deleted or unpublished). Incremental sync alone
        # can't detect a removal — it only sees "what changed" — so without
        # this, the stale row would sit here failing every future rescan
        # too, for up to a week until the next full reconciliation catches
        # it (see content_sync.py). A rescan hitting a real 404 is itself
        # proof enough to clean it up immediately.
        stale_title = post.title
        alerts_r = await db.execute(
            select(Alert).where(
                Alert.site_id == post.site_id, Alert.agent == "optimizer", Alert.type == "content_health",
            )
        )
        for alert in alerts_r.scalars().all():
            if (alert.metadata_ or {}).get("post_id") == post.id:
                await db.delete(alert)
        await db.delete(post)
        await db.commit()
        raise HTTPException(
            status_code=404,
            detail=f'"{stale_title}" no longer exists on WordPress — removed from tracking.',
        )

    post_url = wp_post.get("link") or post.url
    # One live-page fetch yields schema types AND the rendered H1 count
    # (the H1 usually lives in the theme template, outside content.rendered)
    page_signals = await _fetch_page_signals(post_url)

    try:
        health_score, issues, breakdown, word_count, reading_time = _analyze(
            wp_post, site.url,
            extra_schema_types=page_signals["schema_types"],
            live_h1_count=page_signals["h1_count"],
        )
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Analysis failed: {exc}") from exc

    post.health_score = health_score
    post.issues = issues
    post.word_count = word_count
    post.reading_time_minutes = reading_time
    post.score_breakdown = breakdown
    post.last_analyzed_at = datetime.now(timezone.utc)

    import hashlib
    import html as _html_lib
    title_raw = wp_post.get("title", {})
    title_str = title_raw.get("rendered", "") if isinstance(title_raw, dict) else str(title_raw)
    title_str = _html_lib.unescape(_re.sub(r"<[^>]+>", "", title_str)).strip()
    try:
        ai_rec = await asyncio.wait_for(
            _generate_ai_recommendation(
                title_str, health_score, word_count, issues, breakdown,
                site_context=site.site_context or None,
            ),
            timeout=30,
        )
    except Exception:
        ai_rec = None
    # None = generation failed — keep any existing text. "" = genuinely
    # clean — clear the stale recommendation instead of leaving old advice
    # (e.g. a since-fixed "add FAQPage schema") displayed forever.
    if ai_rec is not None:
        post.ai_recommendation = ai_rec or None
        content_text_raw = wp_post.get("content", {})
        content_text = content_text_raw.get("rendered", "") if isinstance(content_text_raw, dict) else ""
        post.ai_rec_hash = hashlib.sha256(
            f"{title_str}|{health_score}|{content_text}".encode()
        ).hexdigest()

    await db.commit()
    return {
        "id": post.id,
        "health_score": post.health_score,
        "word_count": post.word_count,
        "reading_time_minutes": post.reading_time_minutes,
        "issues": post.issues,
        "score_breakdown": post.score_breakdown,
        "ai_recommendation": post.ai_recommendation,
        "last_analyzed_at": post.last_analyzed_at,
    }


@router.post("/content-health/{post_id}/regenerate-ai", dependencies=[Depends(ai_limiter)])
async def regenerate_ai_recommendation(
    post_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    result = await db.execute(select(ContentPost).where(ContentPost.id == post_id))
    post = result.scalar_one_or_none()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    from app.agents.optimizer.content_scorer import _generate_ai_recommendation
    from app.database.models import Site as SiteModel
    site_r = await db.execute(select(SiteModel).where(SiteModel.id == post.site_id))
    site_obj = site_r.scalar_one_or_none()
    ai_rec = await _generate_ai_recommendation(
        post.title, post.health_score, post.word_count, post.issues or [],
        post.score_breakdown or {},
        site_context=site_obj.site_context if site_obj else None,
    )
    # None = generation failed — keep any existing text. "" = genuinely
    # clean — clear it, so a manual "Re-generate" on a fixed post doesn't
    # keep showing advice for an issue that no longer exists.
    if ai_rec is not None:
        post.ai_recommendation = ai_rec or None
        await db.commit()

    return {"ai_recommendation": post.ai_recommendation}


@router.get("/internal-links", response_model=list[InternalLinkResponse])
async def get_internal_links(
    site_id: str | None = None,
    limit: int = Query(200, ge=1, le=500),
    offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    # Ranked by the relevance score InternalLinker computed (IDF-weighted
    # keyword overlap) — not creation time, which is unreliable here since
    # every suggestion from one run lands in the same DB transaction and can
    # share an identical `created_at`.
    relevance_expr = Alert.metadata_["relevance_score"].astext.cast(Float)
    query = (
        select(Alert, Site.name.label("site_name"))
        .join(Site, Alert.site_id == Site.id)
        .where(
            Alert.agent == "optimizer",
            Alert.type == "internal_link",
            Alert.status == "open",
        )
        .order_by(relevance_expr.desc().nullslast(), Alert.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    if site_id:
        query = query.where(Alert.site_id == site_id)

    result = await db.execute(query)
    rows = result.all()

    return [
        {
            "id": alert.id,
            "source_title": (alert.metadata_ or {}).get("source_title", (alert.metadata_ or {}).get("source_page", "")),
            "source_url": (alert.metadata_ or {}).get("source_url", (alert.metadata_ or {}).get("source_page", "")),
            "target_title": (alert.metadata_ or {}).get("target_title", (alert.metadata_ or {}).get("target_page", "")),
            "target_url": (alert.metadata_ or {}).get("target_url", (alert.metadata_ or {}).get("target_page", "")),
            "anchor_text": (alert.metadata_ or {}).get("anchor_text", ""),
            "anchor_source": (alert.metadata_ or {}).get("anchor_source", "title"),
            "anchor_impressions": (alert.metadata_ or {}).get("anchor_impressions"),
            "anchor_clicks": (alert.metadata_ or {}).get("anchor_clicks"),
            "shared_keywords": (alert.metadata_ or {}).get("shared_keywords", []),
            "relevance_score": (alert.metadata_ or {}).get("relevance_score"),
        }
        for alert, site_name in rows
    ]


# ── Flush + Re-run ────────────────────────────────────────────────────────────

class FlushRequest(BaseModel):
    site_id: str | None = None
    module: str = "all"  # "seo" | "content" | "links" | "all"


@router.post("/flush", dependencies=[Depends(job_limiter)])
async def flush_optimizer(
    body: FlushRequest,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    site_id = body.site_id
    module = body.module

    def _base(type_filter: Any) -> Any:
        q = delete(Alert).where(Alert.agent == "optimizer", type_filter)
        if site_id:
            q = q.where(Alert.site_id == site_id)
        return q

    if module in ("seo", "all"):
        await db.execute(_base(Alert.type.like("seo_%")))

    if module in ("content", "all"):
        posts_q = select(ContentPost)
        if site_id:
            posts_q = posts_q.where(ContentPost.site_id == site_id)
        posts_r = await db.execute(posts_q)
        for post in posts_r.scalars().all():
            post.health_score = 50
            post.issues = []
            post.word_count = 0
            post.reading_time_minutes = 0
            post.score_breakdown = {}
            post.ai_recommendation = None
            post.last_analyzed_at = None
        await db.execute(_base(Alert.type == "content_health"))

    if module in ("links", "all"):
        await db.execute(_base(Alert.type == "internal_link"))

    await db.commit()

    # Re-run the relevant agents in background
    background_tasks.add_task(_run_optimizer_module, site_id, module)

    return {"status": "flushed", "site_id": site_id, "module": module}


async def _run_optimizer_module(site_id: str | None, module: str) -> None:
    import logging
    from sqlalchemy import select as sa_select
    from app.database.engine import AsyncSessionLocal
    from app.database.models import Site as SiteModel

    logger = logging.getLogger(__name__)

    if site_id:
        site_ids = [site_id]
    else:
        async with AsyncSessionLocal() as db:
            r = await db.execute(sa_select(SiteModel.id).where(SiteModel.status != "inactive"))
            site_ids = [row[0] for row in r.all()]

    # Fresh session + commit per site — one failing site doesn't lose the rest
    for sid in site_ids:
        async with AsyncSessionLocal() as db:
            try:
                if module in ("seo", "all"):
                    from app.agents.optimizer.seo_analyzer import SEOAnalyzer
                    await SEOAnalyzer(db).run(sid)

                if module in ("content", "all"):
                    from app.agents.optimizer.content_scorer import ContentScorer
                    await ContentScorer(db).run(sid)

                if module in ("links", "all"):
                    from app.agents.optimizer.internal_linker import InternalLinker
                    await InternalLinker(db).run(sid)

                await db.commit()
            except Exception as exc:
                await db.rollback()
                logger.error("Optimizer re-run (%s) failed for site %s: %s", module, sid, exc)

    logger.info("Optimizer module '%s' re-run for site(s) %s", module, site_id or "all")
