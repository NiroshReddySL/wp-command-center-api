import asyncio
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import ColumnElement, Float, delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.engine import get_db
from app.database.models import Alert, ContentPost, PerformanceSnapshot, Site
from app.security.rate_limit import ai_limiter, job_limiter, rescan_limiter

logger = logging.getLogger(__name__)
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
    path = urlparse(url or "").path.strip("/")
    return path.rsplit("/", 1)[-1] if path else None


# URL-slug substrings that identify a site's own Contact/Pricing page — never
# a hardcoded path guess (every site's slugs differ), just a lens over
# whatever pages this site's own ContentPost rows already contain.
_CONVERSION_TARGET_NEEDLES: dict[str, list[str]] = {
    "Contact": ["contact-us", "contact_us", "contact", "get-in-touch", "get-a-quote"],
    "Pricing": ["pricing", "plans-pricing", "price-plans", "prices"],
}


def _detect_conversion_targets(
    posts: list[tuple[str, str, str]], exclude_id: str,
) -> dict[str, tuple[str, str, str]]:
    """Best-guess Contact/Pricing page for this site, found in its own real
    content — `posts` is [(id, title, url), ...] for the site. Prefers the
    shortest matching URL path per category, since the canonical page
    ("/contact/") is always shorter than an incidental blog post that just
    happens to mention the word ("/contact-us-for-a-free-quote-guide/").
    Never matches the post against itself.
    """
    found: dict[str, tuple[str, str, str]] = {}
    for label, needles in _CONVERSION_TARGET_NEEDLES.items():
        candidates = [
            (pid, title, url) for pid, title, url in posts
            if pid != exclude_id and any(n in url.lower() for n in needles)
        ]
        if candidates:
            found[label] = min(candidates, key=lambda c: len(urlparse(c[2]).path))
    return found


# URL-slug substrings for a form-submission confirmation page ("thank you"
# page) — the same detect-from-real-content approach as Contact/Pricing, so
# a redirect-after-submit page turns "reached Contact" into a real,
# attributable "submitted the form" count without hardcoding any site's URL.
_CONFIRMATION_PAGE_NEEDLES = ["thank-you", "thanks", "thank_you", "form-submitted", "confirmation", "success"]


def _detect_confirmation_page(posts: list[tuple[str, str, str]]) -> tuple[str, str, str] | None:
    """The site's own post-submission confirmation page, if one exists —
    used as an optional 3rd funnel step. None if nothing matches, so a site
    without one just keeps the existing "page reached" flows unchanged."""
    candidates = [
        (pid, title, url) for pid, title, url in posts
        if any(n in url.lower() for n in _CONFIRMATION_PAGE_NEEDLES)
    ]
    if not candidates:
        return None
    return min(candidates, key=lambda c: len(urlparse(c[2]).path))


# Google's published Core Web Vitals thresholds: (good_max, needs_work_max).
# A value at or below the first is "good"; at or below the second is
# "needs work"; above it is "poor". Encoded once here rather than as
# magic numbers at each call site so the UI, alerts and any future export
# all rate a metric identically.
#
# `tbt` is Total Blocking Time — a LAB metric PSI reports as a stand-in for
# INP, which can only be measured on real users. It is deliberately NOT
# labelled INP anywhere user-facing.
_CWV_THRESHOLDS: dict[str, tuple[float, float]] = {
    "lcp": (2500.0, 4000.0),   # ms — Largest Contentful Paint
    "cls": (0.1, 0.25),        # unitless — Cumulative Layout Shift
    "tbt": (200.0, 600.0),     # ms — Total Blocking Time
    "ttfb": (800.0, 1800.0),   # ms — Time To First Byte
}

# Lighthouse's own score bands, which PSI colours green/orange/red.
_SPEED_SCORE_GOOD = 90
_SPEED_SCORE_NEEDS_WORK = 50


def _rate_metric(metric: str, value: float | None) -> str | None:
    """"good" | "needs_work" | "poor" for one Core Web Vital, or None when
    the metric is unknown or wasn't measured."""
    if value is None or metric not in _CWV_THRESHOLDS:
        return None
    good_max, needs_work_max = _CWV_THRESHOLDS[metric]
    if value <= good_max:
        return "good"
    if value <= needs_work_max:
        return "needs_work"
    return "poor"


def _rate_speed_score(score: int | None) -> str | None:
    """Lighthouse performance score band. Higher is better here, the
    opposite direction to the timing metrics above."""
    if score is None:
        return None
    if score >= _SPEED_SCORE_GOOD:
        return "good"
    if score >= _SPEED_SCORE_NEEDS_WORK:
        return "needs_work"
    return "poor"


# Typical organic CTR by average position, from published click-through
# studies. These are deliberately COARSE and treated as an order-of-magnitude
# expectation, never a target: real CTR swings hugely with query intent, SERP
# features and brand. They exist only to answer "is this page's CTR wildly
# below what its ranking should earn?" — a question worth asking, and one the
# raw numbers don't answer on their own.
_TYPICAL_CTR_BY_POSITION: list[tuple[float, float]] = [
    (1.0, 28.0), (2.0, 15.0), (3.0, 11.0), (4.0, 8.0), (5.0, 6.0),
    (6.0, 4.5), (7.0, 3.5), (8.0, 3.0), (9.0, 2.5), (10.0, 2.5),
]
# Below this fraction of the typical CTR, the gap is big enough to be a real
# signal rather than normal variance.
_CTR_UNDERPERFORM_RATIO = 0.5
# A page has to actually be seen before "nobody clicks it" means anything.
_CTR_INSIGHT_MIN_IMPRESSIONS = 100

# Positions 4-15: ranking, but below the results that get the clicks. Small
# improvements here move the most traffic, which is why they're worth
# surfacing separately from queries already at #1.
_STRIKING_MIN_POSITION = 4.0
_STRIKING_MAX_POSITION = 15.0
_STRIKING_MIN_IMPRESSIONS = 10


def _typical_ctr(position: float) -> float | None:
    """Roughly the CTR a result at this average position tends to earn.
    None past position 10, where rates flatten into noise and any specific
    number would be false precision."""
    if position <= 0:
        return None
    for max_pos, ctr in _TYPICAL_CTR_BY_POSITION:
        if position <= max_pos:
            return ctr
    return None


def _ctr_opportunity(
    position: float, ctr: float, impressions: int,
) -> dict[str, Any] | None:
    """Flags a page that ranks well but is rarely clicked — the signature of
    a title/meta-description problem rather than a ranking problem, and the
    single most actionable thing GSC can tell a content editor.

    Returns None unless the page has enough impressions to judge, sits in
    the range where typical CTR is meaningful, and falls far enough short to
    be a signal rather than noise.
    """
    if impressions < _CTR_INSIGHT_MIN_IMPRESSIONS:
        return None
    expected = _typical_ctr(position)
    if expected is None or ctr >= expected * _CTR_UNDERPERFORM_RATIO:
        return None
    # Clicks this page would earn at a typical CTR for its current ranking —
    # framed as the gap at TODAY's position, so it never reads as a promise
    # that comes from ranking higher.
    potential = int(impressions * expected / 100)
    return {
        "position": position,
        "ctr": ctr,
        "typical_ctr": expected,
        "potential_clicks": potential,
    }


def _striking_distance(queries: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Queries ranking just below the click-earning positions, with enough
    impressions that improving them would actually matter."""
    return [
        q for q in queries
        if _STRIKING_MIN_POSITION <= q["position"] <= _STRIKING_MAX_POSITION
        and q["impressions"] >= _STRIKING_MIN_IMPRESSIONS
    ]


# Ordered so the stacked bar and legend always read desktop → mobile →
# tablet regardless of which happened to win, and so a colour never moves
# between categories when the ranking changes.
_DEVICE_ORDER = ["desktop", "mobile", "tablet"]


def _device_shares(counts: dict[str, int]) -> list[dict[str, Any]]:
    """Device categories as [{device, users, pct}], biggest-share info kept
    in a fixed category order.

    Percentages are computed against the total of everything GA4 returned —
    including categories outside _DEVICE_ORDER (GA4 occasionally reports
    "smart tv") — so the parts always describe the real whole rather than
    summing past 100%. Any such extra category is folded into a final
    "other" row rather than silently dropped.
    """
    total = sum(counts.values())
    if total <= 0:
        return []

    rows: list[dict[str, Any]] = []
    for device in _DEVICE_ORDER:
        users = counts.get(device, 0)
        if users > 0:
            rows.append({"device": device, "users": users, "pct": round(users / total * 100, 1)})

    other = sum(v for k, v in counts.items() if k not in _DEVICE_ORDER)
    if other > 0:
        rows.append({"device": "other", "users": other, "pct": round(other / total * 100, 1)})
    return rows


def _page_location_regex(path: str) -> str:
    """A GA4 FULL_REGEXP matching exactly this page and nothing else.

    The funnel used to match page_location with CONTAINS, which quietly
    swept in any URL merely *containing* the path — on a real site that
    included "/some-post/>", a malformed URL produced by broken markup
    (href="...post/>"). Those visitors hit a 404, never read the post, and
    still inflated the funnel's entry count above the traffic tile's,
    which matches pagePath exactly.

    Anchored to the whole URL (FULL_REGEXP must match end-to-end), with an
    optional trailing slash, query string and fragment so genuine
    "?utm_source=..." visits still count.
    """
    core = re.escape(path.rstrip("/"))
    return rf"https?://[^/]+{core}/?(\?.*)?(#.*)?"


def _pct_change(previous: int, current: int) -> float | None:
    """None when there's no real baseline to compare against (0 in the
    prior period) — a manufactured "+100%"/infinite% would be misleading."""
    if previous == 0:
        return None
    return round((current - previous) / previous * 100, 1)


def _fill_daily_gaps(day_counts: dict[str, int], start_date: str, end_date: str) -> list[dict[str, Any]]:
    """GA4 omits days with zero activity entirely rather than returning a
    zero row — reconciled here against every real calendar date in range so
    a quiet day reads as 0 on the chart, not as a gap."""
    start = datetime.strptime(start_date, "%Y-%m-%d").date()
    end = datetime.strptime(end_date, "%Y-%m-%d").date()
    points: list[dict[str, Any]] = []
    day = start
    while day <= end:
        iso = day.isoformat()
        points.append({"date": iso, "views": day_counts.get(iso, 0)})
        day += timedelta(days=1)
    return points


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
    ai_guidance: dict | None = None


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
    ai_guidance: dict | None = None


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
        "ai_guidance": post.ai_guidance,
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


async def _resolve_post_ref(
    ref: str, site_id: str | None, db: AsyncSession,
) -> tuple[ContentPost, str]:
    """Fetch one post by UUID or by its WP slug (clean URLs).

    Slug lookups can collide across sites — pass site_id to disambiguate;
    otherwise the most recently analyzed match wins. Shared by every
    per-post endpoint below so "how a post is looked up" only lives once.
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
    return row


@router.get("/content-health/{ref}", response_model=ContentHealthDetailResponse)
async def get_content_post_detail(
    ref: str,
    site_id: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    post, site_name = await _resolve_post_ref(ref, site_id, db)
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
        "ai_guidance": post.ai_guidance,
    }


class DailyTrafficPoint(BaseModel):
    date: str
    views: int


class ConversionFlow(BaseModel):
    label: str
    target_title: str
    target_url: str
    entered: int
    reached: int
    reach_rate: float  # 0-1, reached/entered
    submitted: int | None = None  # None when no confirmation page was detected for this site
    submission_rate: float | None = None  # 0-1, submitted/entered


class DeviceShare(BaseModel):
    device: str  # "desktop" | "mobile" | "tablet" | "other"
    users: int
    pct: float  # 0-100


class ContentAnalyticsResponse(BaseModel):
    connected: bool
    daily_traffic: list[DailyTrafficPoint] = []
    # Sum of per-day active users — i.e. VISITS (a person returning on three
    # days counts three times). Drives the chart; deliberately distinct from
    # `visitors_30d` below, which the funnel is built on.
    traffic_30d: int = 0
    traffic_prev_30d: int = 0
    traffic_change_pct: float | None = None
    # Unique PEOPLE over the window, deduplicated by GA4 across the whole
    # range. This is what every funnel step counts, so it's reported
    # separately rather than letting the UI imply the visit total and the
    # funnel's entry count should match — they measure different things and
    # showing them side by side as one number was the reported bug.
    visitors_30d: int = 0
    bounce_rate: float | None = None  # 0-100
    avg_engagement_time: float | None = None  # seconds
    # How this post's readers split across device categories — the context
    # that says which PageSpeed strategy actually reflects your audience.
    devices: list[DeviceShare] = []
    flows: list[ConversionFlow] = []
    # Unique people who read this post and later reached the confirmation
    # page, measured DIRECTLY — never the sum of `submitted` across flows,
    # which double-counts anyone whose journey satisfied more than one route
    # (they all end at the same confirmation page). None if no confirmation
    # page was detected for this site.
    total_leads: int | None = None
    error: str | None = None


class PageSpeedMetric(BaseModel):
    key: str
    label: str
    value: float
    unit: str          # "ms" | "" (CLS is unitless)
    rating: str | None  # "good" | "needs_work" | "poor"


class PageSpeedResponse(BaseModel):
    # False when this page has never been tested — the UI offers to run one
    # rather than showing an error, since PSI is opt-in per page.
    tested: bool
    score: int | None = None
    rating: str | None = None
    strategy: str = "desktop"
    metrics: list[PageSpeedMetric] = []
    tested_at: datetime | None = None
    page_url: str | None = None
    error: str | None = None


def _serialize_pagespeed(snap: PerformanceSnapshot) -> dict[str, Any]:
    """One stored snapshot as the API's metric list. `fid` holds Total
    Blocking Time (see PerformanceMonitor) — surfaced under its real name so
    nobody reads it as a field-measured INP."""
    metrics = [
        ("lcp", "Largest Contentful Paint", snap.lcp, "ms"),
        ("cls", "Cumulative Layout Shift", snap.cls, ""),
        ("tbt", "Total Blocking Time", snap.fid, "ms"),
        ("ttfb", "Time to First Byte", snap.ttfb, "ms"),
    ]
    return {
        "tested": True,
        "score": snap.speed_score,
        "rating": _rate_speed_score(snap.speed_score),
        "strategy": snap.strategy,
        "page_url": snap.page_url,
        "tested_at": snap.snapshot_at,
        "metrics": [
            {
                "key": key, "label": label, "value": round(value, 3),
                "unit": unit, "rating": _rate_metric(key, value),
            }
            for key, label, value, unit in metrics
        ],
    }


async def _latest_pagespeed(
    page_url: str, strategy: str, db: AsyncSession,
) -> PerformanceSnapshot | None:
    result = await db.execute(
        select(PerformanceSnapshot)
        .where(
            PerformanceSnapshot.page_url == page_url,
            PerformanceSnapshot.strategy == strategy,
        )
        .order_by(PerformanceSnapshot.snapshot_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


class SearchQueryRow(BaseModel):
    query: str
    clicks: int
    impressions: int
    ctr: float       # 0-100
    position: float


class SearchDailyPoint(BaseModel):
    date: str
    clicks: int
    impressions: int


class CtrOpportunity(BaseModel):
    position: float
    ctr: float
    typical_ctr: float
    potential_clicks: int


class SearchConsoleResponse(BaseModel):
    connected: bool
    range_start: str | None = None
    range_end: str | None = None
    clicks: int = 0
    impressions: int = 0
    ctr: float = 0.0        # 0-100
    position: float = 0.0
    # Deltas vs the immediately preceding window of equal length. Position is
    # the one metric where a NEGATIVE change is an improvement.
    clicks_change_pct: float | None = None
    impressions_change_pct: float | None = None
    position_change: float | None = None
    daily: list[SearchDailyPoint] = []
    queries: list[SearchQueryRow] = []
    striking_distance: list[SearchQueryRow] = []
    ctr_opportunity: CtrOpportunity | None = None
    error: str | None = None


# Search Console finalizes data ~2-3 days late. Ending the window there
# rather than "today" keeps the current period as complete as the one it's
# compared against — otherwise every page would look like it was declining,
# purely because the newest days hadn't landed yet.
_GSC_LAG_DAYS = 3
_GSC_WINDOW_DAYS = 28


@router.get("/content-health/{ref}/search-console", response_model=SearchConsoleResponse)
async def get_content_search_console(
    ref: str,
    site_id: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Google Search organic performance for one post: totals vs the previous
    period, a daily trend, the queries it actually surfaces for, and the two
    insights worth acting on — a CTR far below what its ranking should earn,
    and queries sitting just outside the click-earning positions.

    Degrades gracefully (200 with `connected: false` / `error`) — this is a
    supplementary panel, not the page's core data.
    """
    from app.api.auth import get_google_token
    from app.connectors.search_console import SearchConsoleConnector
    from app.database.models import SiteConfig

    post, _site_name = await _resolve_post_ref(ref, site_id, db)

    cfg_r = await db.execute(select(SiteConfig).where(SiteConfig.site_id == post.site_id))
    cfg = cfg_r.scalar_one_or_none()
    site = await db.get(Site, post.site_id)
    token = await get_google_token(db)
    gsc_url = (cfg.gsc_site_url if cfg else None) or (site.url if site else None)
    if not token or not gsc_url:
        return {"connected": False}

    end = datetime.now(UTC).date() - timedelta(days=_GSC_LAG_DAYS)
    start = end - timedelta(days=_GSC_WINDOW_DAYS - 1)
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=_GSC_WINDOW_DAYS - 1)

    try:
        gsc = SearchConsoleConnector(token.access_token)
        summary, previous, daily, queries = await asyncio.gather(
            gsc.get_page_search_summary(gsc_url, post.url, str(start), str(end)),
            gsc.get_page_search_summary(gsc_url, post.url, str(prev_start), str(prev_end)),
            gsc.get_page_daily_search(gsc_url, post.url, str(start), str(end)),
            gsc.get_page_query_details(gsc_url, post.url, str(start), str(end)),
        )
    except Exception as exc:
        return {"connected": True, "error": f"Couldn't load Search Console data: {exc}"}

    return {
        "connected": True,
        "range_start": str(start),
        "range_end": str(end),
        **summary,
        "clicks_change_pct": _pct_change(previous["clicks"], summary["clicks"]),
        "impressions_change_pct": _pct_change(previous["impressions"], summary["impressions"]),
        # A raw difference, not a percentage: "position improved by 1.4" is
        # meaningful where "position improved 18%" is not.
        "position_change": (
            round(summary["position"] - previous["position"], 1)
            if previous["position"] and summary["position"] else None
        ),
        "daily": daily,
        "queries": queries[:15],
        "striking_distance": _striking_distance(queries)[:8],
        "ctr_opportunity": _ctr_opportunity(
            summary["position"], summary["ctr"], summary["impressions"],
        ),
    }


@router.get("/content-health/{ref}/pagespeed", response_model=PageSpeedResponse)
async def get_content_pagespeed(
    ref: str,
    site_id: str | None = None,
    strategy: str = Query("desktop", pattern="^(mobile|desktop)$"),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """The most recent stored PageSpeed result for this post, if any.

    Deliberately never calls PSI itself — a live run takes 10-30s, which is
    far too slow to sit in a page load. This returns instantly (cached or
    `tested: false`) and the POST below runs the real test on request.
    """
    post, _site_name = await _resolve_post_ref(ref, site_id, db)
    snap = await _latest_pagespeed(post.url, strategy, db)
    if snap is None:
        return {"tested": False, "strategy": strategy, "page_url": post.url}
    return _serialize_pagespeed(snap)


@router.post(
    "/content-health/{ref}/pagespeed",
    response_model=PageSpeedResponse,
    dependencies=[Depends(job_limiter)],
)
async def run_content_pagespeed(
    ref: str,
    site_id: str | None = None,
    strategy: str = Query("desktop", pattern="^(mobile|desktop)$"),
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Run a live PageSpeed Insights test for this post and store the result.

    Slow by nature (PSI runs a real Lighthouse pass), hence rate-limited and
    user-triggered rather than part of any automatic sweep.
    """
    import httpx

    from app.agents.watchdog.performance import _fetch_psi

    post, _site_name = await _resolve_post_ref(ref, site_id, db)

    async with httpx.AsyncClient(timeout=60.0) as client:
        result = await _fetch_psi(client, post.url, strategy=strategy)

    if result is None:
        # Surfaced as a normal 200 with `error` so the card can show a retry
        # affordance; a 5xx here would read as "the app is broken" when the
        # real story is "Google didn't answer this time".
        return {
            "tested": False, "strategy": strategy, "page_url": post.url,
            "error": "PageSpeed Insights didn't return a result — it may be rate limited. Try again shortly.",
        }

    snapshot = PerformanceSnapshot(
        site_id=post.site_id,
        page_url=post.url,
        lcp=result["lcp"],
        cls=result["cls"],
        fid=result["fid"],
        ttfb=result["ttfb"],
        speed_score=result["score"],
        strategy=strategy,
    )
    db.add(snapshot)
    await db.commit()
    await db.refresh(snapshot)
    return _serialize_pagespeed(snapshot)


@router.get("/content-health/{ref}/analytics", response_model=ContentAnalyticsResponse)
async def get_content_post_analytics(
    ref: str,
    site_id: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Real GA4 analytics for one post: a day-by-day 30-day traffic chart
    (vs. the previous 30 days), bounce rate/engagement time, and how many of
    this post's visitors went on to reach the site's own Contact/Pricing
    page. When the site has a form-submission confirmation ("thank you")
    page, the funnel extends to it as a 3rd step, so `reached` (got to the
    Contact/Pricing page) and `submitted` (actually completed the form) are
    reported as distinct, real conversion counts rather than one proxy
    number. Degrades gracefully — not connected, or a live GA4 error, both
    return a normal 200 with `connected`/`error` set rather than a 500,
    since this is a supplementary section of the page, not its core data.
    """
    from app.api.auth import get_google_token
    from app.connectors.analytics import AnalyticsConnector
    from app.database.models import SiteConfig

    post, _site_name = await _resolve_post_ref(ref, site_id, db)

    cfg_r = await db.execute(select(SiteConfig).where(SiteConfig.site_id == post.site_id))
    cfg = cfg_r.scalar_one_or_none()
    token = await get_google_token(db)
    if not cfg or not cfg.ga_property_id or not token:
        return {"connected": False}

    today = datetime.now(UTC).date()
    range_start = (today - timedelta(days=29)).isoformat()
    range_end = today.isoformat()
    prev_start = (today - timedelta(days=59)).isoformat()
    prev_end = (today - timedelta(days=30)).isoformat()
    post_path = urlparse(post.url).path

    try:
        ga = AnalyticsConnector(token.access_token)

        daily_by_path = await ga.get_daily_active_users_by_path(
            cfg.ga_property_id, [post_path], prev_start, range_end,
        )
        day_counts = daily_by_path.get(post_path, {})
        daily_traffic = _fill_daily_gaps(day_counts, range_start, range_end)
        traffic_30d = sum(p["views"] for p in daily_traffic)
        prev_points = _fill_daily_gaps(day_counts, prev_start, prev_end)
        traffic_prev_30d = sum(p["views"] for p in prev_points)

        engagement, device_counts = await asyncio.gather(
            ga.get_engagement_metrics_by_path(
                cfg.ga_property_id, [post_path], range_start, range_end,
            ),
            ga.get_device_breakdown_by_path(
                cfg.ga_property_id, [post_path], range_start, range_end,
            ),
        )
        engagement = engagement.get(post_path)
        devices = _device_shares(device_counts)

        all_posts = list((await db.execute(
            select(ContentPost.id, ContentPost.title, ContentPost.url)
            .where(ContentPost.site_id == post.site_id)
        )).all())
        targets = _detect_conversion_targets(all_posts, exclude_id=post.id)
        confirmation = _detect_confirmation_page(all_posts)

        # Anchored so a malformed URL that merely CONTAINS this path (e.g.
        # "/post/>" from broken markup) can't be counted as reading the post.
        post_step = {"label": post.title[:60], "match_type": "regex", "pattern": _page_location_regex(post_path)}

        flows: list[dict[str, Any]] = []
        for label, (_target_id, target_title, target_url) in targets.items():
            steps = [
                post_step,
                {"label": label, "match_type": "contains", "pattern": urlparse(target_url).path},
            ]
            if confirmation:
                steps.append({
                    "label": "Submitted", "match_type": "contains",
                    "pattern": urlparse(confirmation[2]).path,
                })

            funnel = await ga.run_funnel_report(
                cfg.ga_property_id, steps=steps, start_date=range_start, end_date=range_end,
            )
            entered = funnel["step_results"][0]["active_users"]
            reached = funnel["step_results"][1]["active_users"]
            submitted = funnel["step_results"][2]["active_users"] if confirmation else None

            flows.append({
                "label": label,
                "target_title": target_title,
                "target_url": target_url,
                "entered": entered,
                "reached": reached,
                "reach_rate": (reached / entered) if entered else 0.0,
                "submitted": submitted,
                "submission_rate": (submitted / entered) if confirmation and entered else None,
            })

        # Unique converters, measured directly. Every route above ends at the
        # same confirmation page, so a visitor who passed through both
        # Contact AND Pricing appears in both flows — summing them reported
        # one real person as two leads.
        visitors_30d = flows[0]["entered"] if flows else 0
        total_leads = None
        if confirmation:
            lead_funnel = await ga.run_funnel_report(
                cfg.ga_property_id,
                steps=[
                    post_step,
                    {"label": "Submitted", "match_type": "contains",
                     "pattern": urlparse(confirmation[2]).path},
                ],
                start_date=range_start, end_date=range_end,
            )
            visitors_30d = lead_funnel["step_results"][0]["active_users"]
            total_leads = lead_funnel["step_results"][1]["active_users"]

        return {
            "connected": True,
            "daily_traffic": daily_traffic,
            "traffic_30d": traffic_30d,
            "traffic_prev_30d": traffic_prev_30d,
            "traffic_change_pct": _pct_change(traffic_prev_30d, traffic_30d),
            "visitors_30d": visitors_30d,
            "bounce_rate": round(engagement["bounce_rate"] * 100, 1) if engagement else None,
            "avg_engagement_time": round(engagement["avg_engagement_time"], 1) if engagement else None,
            "devices": devices,
            "flows": flows,
            "total_leads": total_leads,
        }
    except Exception as exc:
        return {"connected": True, "error": f"Couldn't load live analytics: {exc}"}


@router.post("/content-health/{post_id}/rescan", dependencies=[Depends(rescan_limiter)])
async def rescan_content_post(
    post_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Re-fetch from WordPress and re-run deep analysis for a single post."""
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

    import re as _re

    from app.agents.optimizer.content_scorer import (
        _analyze,
        _fetch_page_signals,
        _generate_ai_recommendation,
    )
    from app.connectors.wordpress import WordPressConnector

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
    post.last_analyzed_at = datetime.now(UTC)

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


class PageInsight(BaseModel):
    id: str
    source: str      # "content" | "traffic" | "search" | "speed"
    severity: str    # "critical" | "warning" | "info"
    title: str
    detail: str
    action: str
    evidence: str
    impact: int


class InsightSummary(BaseModel):
    visitors: int | None = None
    leads: int | None = None
    search_clicks: int | None = None
    search_position: float | None = None
    speed_score: int | None = None
    speed_strategy: str | None = None


class PageInsightsResponse(BaseModel):
    summary: InsightSummary
    insights: list[PageInsight]
    # Which sources actually contributed, so the UI can say "connect Search
    # Console for more" rather than silently showing a thinner analysis.
    sources: list[str]


@router.get("/content-health/{ref}/insights", response_model=PageInsightsResponse)
async def get_content_insights(
    ref: str,
    site_id: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Rule-based findings across content, traffic, search and speed, ranked
    by severity and estimated impact.

    Deliberately involves NO AI: this is the page's baseline analysis, shown
    on every visit, so it has to be instant, free and identical for identical
    data. The AI layer is a separate, explicitly-requested call that builds
    on these findings.

    Returns the headline numbers alongside the findings so the overview needs
    exactly one request rather than one per data source.
    """
    from app.agents.optimizer.insights import build_insights

    post, _site_name = await _resolve_post_ref(ref, site_id, db)
    perf = await _collect_post_performance(post, db)

    sources: list[str] = []
    traffic: dict[str, Any] | None = None
    if perf.get("visitors_30d") is not None or perf.get("bounce_rate") is not None:
        sources.append("traffic")
        traffic = {
            "visitors": perf.get("visitors_30d"),
            "bounce_rate": perf.get("bounce_rate"),
            "avg_engagement_time": perf.get("avg_engagement_time"),
            "leads": perf.get("leads"),
        }

    search: dict[str, Any] | None = None
    if perf.get("search_impressions") is not None:
        sources.append("search")
        search = {
            "clicks": perf.get("search_clicks"),
            "position_change": perf.get("search_position_change"),
            "ctr_opportunity": perf.get("ctr_opportunity"),
            "striking_distance": perf.get("top_queries_striking") or [],
        }

    speed: dict[str, Any] | None = None
    if perf.get("speed_score") is not None:
        sources.append("speed")
        speed = {
            "score": perf.get("speed_score"),
            "strategy": perf.get("speed_strategy"),
            "failing_vitals": perf.get("failing_vitals"),
            "visitors": perf.get("visitors_30d") or 0,
        }

    breakdown = post.score_breakdown or {}
    if breakdown:
        sources.append("content")

    return {
        "summary": {
            "visitors": perf.get("visitors_30d"),
            "leads": perf.get("leads"),
            "search_clicks": perf.get("search_clicks"),
            "search_position": perf.get("search_position"),
            "speed_score": perf.get("speed_score"),
            "speed_strategy": perf.get("speed_strategy"),
        },
        "insights": build_insights(
            breakdown=breakdown, traffic=traffic, search=search, speed=speed,
            issues=post.issues or [],
        ),
        "sources": sources,
    }


async def _collect_post_performance(post: ContentPost, db: AsyncSession) -> dict[str, Any]:
    """Everything measured about one page, for the AI recommendation prompt.

    Each source is gathered independently and failures are swallowed per
    source: a site with no GA4, an unverified Search Console property, or a
    page never speed-tested must still get a recommendation — just a less
    informed one. Search Console is the highest-value source here, since its
    real query wording is what lets the model rewrite a title to match how
    people actually search.

    Reads the STORED PageSpeed snapshot rather than running a live test —
    a live PSI pass takes ~20s, far too slow to sit inside a button click
    that the user is waiting on.
    """
    from app.api.auth import get_google_token
    from app.connectors.analytics import AnalyticsConnector
    from app.connectors.search_console import SearchConsoleConnector
    from app.database.models import SiteConfig

    perf: dict[str, Any] = {}
    if post.traffic_30d:
        perf["visitors_30d"] = post.traffic_30d

    cfg_r = await db.execute(select(SiteConfig).where(SiteConfig.site_id == post.site_id))
    cfg = cfg_r.scalar_one_or_none()
    site = await db.get(Site, post.site_id)
    token = await get_google_token(db)

    # ── GA4 engagement + conversions ──────────────────────────────────────
    if token and cfg and cfg.ga_property_id:
        post_path = urlparse(post.url).path
        today = datetime.now(UTC).date()
        start, end = (today - timedelta(days=29)).isoformat(), today.isoformat()
        try:
            ga = AnalyticsConnector(token.access_token)
            engagement = (await ga.get_engagement_metrics_by_path(
                cfg.ga_property_id, [post_path], start, end,
            )).get(post_path)
            if engagement:
                perf["bounce_rate"] = engagement["bounce_rate"] * 100
                perf["avg_engagement_time"] = engagement["avg_engagement_time"]

            # Conversions: the same deduplicated post → confirmation-page
            # measurement the Traffic tab reports, in one funnel call.
            all_posts = list((await db.execute(
                select(ContentPost.id, ContentPost.title, ContentPost.url)
                .where(ContentPost.site_id == post.site_id)
            )).all())
            confirmation = _detect_confirmation_page(all_posts)
            if confirmation:
                funnel = await ga.run_funnel_report(
                    cfg.ga_property_id,
                    steps=[
                        {"label": "post", "match_type": "regex",
                         "pattern": _page_location_regex(post_path)},
                        {"label": "converted", "match_type": "contains",
                         "pattern": urlparse(confirmation[2]).path},
                    ],
                    start_date=start, end_date=end,
                )
                perf["visitors_30d"] = funnel["step_results"][0]["active_users"]
                perf["leads"] = funnel["step_results"][1]["active_users"]
        except Exception as exc:
            logger.info("GA4 enrichment unavailable for %s: %s", post.url, exc)

    # ── Search Console ────────────────────────────────────────────────────
    gsc_url = (cfg.gsc_site_url if cfg else None) or (site.url if site else None)
    if token and gsc_url:
        try:
            gsc = SearchConsoleConnector(token.access_token)
            end = datetime.now(UTC).date() - timedelta(days=_GSC_LAG_DAYS)
            start = end - timedelta(days=_GSC_WINDOW_DAYS - 1)
            prev_end = start - timedelta(days=1)
            prev_start = prev_end - timedelta(days=_GSC_WINDOW_DAYS - 1)
            summary, previous, queries = await asyncio.gather(
                gsc.get_page_search_summary(gsc_url, post.url, str(start), str(end)),
                gsc.get_page_search_summary(gsc_url, post.url, str(prev_start), str(prev_end)),
                gsc.get_page_query_details(gsc_url, post.url, str(start), str(end)),
            )
            if summary["impressions"]:
                striking = _striking_distance(queries)
                perf["search_clicks"] = summary["clicks"]
                perf["search_impressions"] = summary["impressions"]
                perf["search_ctr"] = summary["ctr"]
                perf["search_position"] = summary["position"]
                perf["search_position_change"] = (
                    round(summary["position"] - previous["position"], 1)
                    if previous["position"] and summary["position"] else None
                )
                perf["ctr_opportunity"] = _ctr_opportunity(
                    summary["position"], summary["ctr"], summary["impressions"],
                )
                perf["top_queries_striking"] = striking
                # Striking-distance queries first — those are the ones worth
                # writing for; fall back to plain top queries if none qualify.
                perf["top_queries"] = striking[:5] or queries[:5]
                # The unabridged list: the guidance pass reads the page
                # against real demand, so it needs the full picture rather
                # than the handful the prompt summary shows.
                perf["all_queries"] = queries
        except Exception as exc:
            logger.info("Search Console enrichment unavailable for %s: %s", post.url, exc)

    # ── PageSpeed (stored, never a live run) ──────────────────────────────
    snap = await _latest_pagespeed(post.url, "desktop", db)
    if snap is None:
        snap = await _latest_pagespeed(post.url, "mobile", db)
    if snap is not None:
        perf["speed_score"] = snap.speed_score
        perf["speed_strategy"] = snap.strategy
        perf["failing_vitals"] = [
            label
            for key, label, value in (
                ("lcp", "LCP", snap.lcp), ("cls", "CLS", snap.cls),
                ("tbt", "TBT", snap.fid), ("ttfb", "TTFB", snap.ttfb),
            )
            if _rate_metric(key, value) in ("needs_work", "poor")
        ]

    return perf


@router.post("/content-health/{post_id}/regenerate-ai", dependencies=[Depends(ai_limiter)])
async def regenerate_ai_recommendation(
    post_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    result = await db.execute(select(ContentPost).where(ContentPost.id == post_id))
    post = result.scalar_one_or_none()
    if not post:
        raise HTTPException(status_code=404, detail="Post not found")

    from app.agents.optimizer.guidance import generate_page_guidance, guidance_as_text
    from app.agents.optimizer.insights import build_insights
    from app.connectors.wordpress import WordPressConnector
    from app.database.models import Site as SiteModel

    site_r = await db.execute(select(SiteModel).where(SiteModel.id == post.site_id))
    site_obj = site_r.scalar_one_or_none()
    performance = await _collect_post_performance(post, db)

    # The page's real text — the whole point of this pass. Fetched live
    # because only the content HASH is stored, never the body. A fetch
    # failure degrades to metrics-and-queries rather than failing the
    # request; the guidance is just less grounded.
    content_html = ""
    if post.wp_post_id and site_obj:
        wp = WordPressConnector(site_obj.url, site_obj.api_key)
        try:
            wp_post = await wp.get_post(post.wp_post_id, content_type=post.content_type)
            content_html = (wp_post or {}).get("content", {}).get("rendered", "") or ""
        except Exception as exc:
            logger.info("Couldn't fetch live content for %s: %s", post.url, exc)
        finally:
            await wp.close()

    breakdown = post.score_breakdown or {}
    known = build_insights(
        breakdown=breakdown,
        traffic={
            "visitors": performance.get("visitors_30d"),
            "bounce_rate": performance.get("bounce_rate"),
            "avg_engagement_time": performance.get("avg_engagement_time"),
            "leads": performance.get("leads"),
        },
        search={
            "clicks": performance.get("search_clicks"),
            "ctr_opportunity": performance.get("ctr_opportunity"),
            "striking_distance": performance.get("top_queries_striking") or [],
            "position_change": performance.get("search_position_change"),
        },
        speed={
            "score": performance.get("speed_score"),
            "strategy": performance.get("speed_strategy"),
            "failing_vitals": performance.get("failing_vitals"),
            "visitors": performance.get("visitors_30d") or 0,
        } if performance.get("speed_score") is not None else None,
        issues=post.issues or [],
    )

    guidance = await generate_page_guidance(
        title=post.title,
        url=post.url,
        content_html=content_html,
        meta_description=(breakdown.get("meta_description") or {}).get("preview"),
        metrics={
            "Visitors (30d)": performance.get("visitors_30d"),
            "Bounce rate": f"{performance['bounce_rate']:.0f}%" if performance.get("bounce_rate") is not None else None,
            "Avg engagement": f"{performance['avg_engagement_time']:.0f}s" if performance.get("avg_engagement_time") is not None else None,
            "Search clicks (28d)": performance.get("search_clicks"),
            "Search impressions (28d)": performance.get("search_impressions"),
            "Search CTR": f"{performance['search_ctr']:.2f}%" if performance.get("search_ctr") is not None else None,
            "Avg position": performance.get("search_position"),
            "Word count": post.word_count,
            "Health score": f"{post.health_score}/100",
            "PageSpeed": performance.get("speed_score"),
        },
        queries=performance.get("all_queries") or performance.get("top_queries") or [],
        known_findings=[f"{i['title']} — {i['detail']}" for i in known],
        site_context=site_obj.site_context if site_obj else None,
    )

    # None = generation failed — leave whatever is stored alone rather than
    # wiping guidance that may still be accurate.
    if guidance is not None:
        post.ai_guidance = guidance
        post.ai_recommendation = guidance_as_text(guidance) or None
        await db.commit()

    return {
        "ai_recommendation": post.ai_recommendation,
        "ai_guidance": post.ai_guidance,
        "generation_failed": guidance is None,
    }


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
