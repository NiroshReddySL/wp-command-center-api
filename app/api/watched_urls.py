"""Watched URLs — user-curated list of pages tracked for live GA4 active users.

See the WatchedUrl model docstring for the path/title/url distinction:
`path` is the internal identity (dedup + analytics matching), `url` is the
full link kept purely for display, and `title` is resolved separately since
GA4's Realtime API can only be joined by page title, not path.

Requires the site to have a connected Google Analytics property to show live
counts; without one, entries are still listed (added/managed) with
active_users always 0 and `ga_connected: false` so the UI can explain why.
"""
import asyncio
import csv
import html as _html
import io
import logging
import re
from datetime import date, datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlparse

import httpx
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.engine import get_db
from app.database.models import ContentPost, Site, SiteConfig, WatchedUrl
from app.security.rate_limit import job_limiter

logger = logging.getLogger(__name__)
router = APIRouter()

_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.DOTALL)
# Exact header match (case-insensitive) — wherever this column sits, it wins.
_URL_HEADER_NAMES = {"url", "urls"}
# Fallback header names recognized only in the first column, when no
# dedicated URL/URLs column is found (see _parse_csv_urls).
_CSV_HEADER_NAMES = _URL_HEADER_NAMES | {"link", "links", "page", "path", "page url", "pageurl"}
_MAX_URLS_PER_REQUEST = 500
_MAX_CSV_BYTES = 2 * 1024 * 1024  # 2MB — plenty for tens of thousands of rows

# GA4-style date range presets — "realtime" is the one exception (Active
# Users right now, via the Realtime API); every other key resolves to REAL
# calendar dates (not GA4's "NdaysAgo"/"today" relative keywords) so the
# actual range is always known — needed to label the "Active Users" column,
# name export files after the real dates instead of the preset key, and
# enumerate exact calendar days for a day-wise breakdown.
_RANGE_KEYS = frozenset({"today", "yesterday", "7d", "28d", "90d", "qtd", "ytd", "custom"})
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def _quarter_start(today: date) -> date:
    first_month_of_quarter = ((today.month - 1) // 3) * 3 + 1
    return date(today.year, first_month_of_quarter, 1)


# ── Normalization ──────────────────────────────────────────────────────────────

def _normalize(raw: str, site_url: str) -> tuple[str, str]:
    """Return (full_url, path) from either a full URL or a bare path/slug.

    A full URL must belong to the site's own domain: this feature tracks
    pages on THIS site, and it means every later fetch targets a host the
    site owner already proved they control when adding the site — never an
    arbitrary user-supplied host.
    """
    raw = raw.strip()
    if not raw:
        raise ValueError("Empty URL")

    if raw.startswith("http://") or raw.startswith("https://"):
        parsed = urlparse(raw)
        site_host = urlparse(site_url).hostname
        if parsed.hostname != site_host:
            raise ValueError(f'"{raw}" is not a page on {site_host}')
        path = parsed.path or "/"
    else:
        path = raw if raw.startswith("/") else f"/{raw}"

    base = site_url.rstrip("/")
    return f"{base}{path}", path


def _parse_csv_urls(content: bytes) -> list[str]:
    """Reads URLs from a CSV.

    If the first row has a column literally named "url"/"URL"/"urls"/"URLs"
    etc. (case-insensitive), that column is read regardless of where it
    sits — a "notes, URL, owner" file works the same as a "URL, notes" one.
    Otherwise falls back to the first column, skipping a recognized header
    row there (e.g. "Link", "Page"). Handles Excel's UTF-8 BOM.
    """
    text = content.decode("utf-8-sig", errors="ignore")
    rows = list(csv.reader(io.StringIO(text)))
    if not rows:
        return []

    header = [cell.strip().lower() for cell in rows[0]]
    url_col = next((i for i, cell in enumerate(header) if cell in _URL_HEADER_NAMES), None)

    if url_col is not None:
        col, data_rows = url_col, rows[1:]
    else:
        col = 0
        data_rows = rows[1:] if header and header[0] in _CSV_HEADER_NAMES else rows

    urls: list[str] = []
    for row in data_rows:
        if len(row) > col:
            value = row[col].strip()
            if value:
                urls.append(value)
    return urls


# ── Title resolution (for matching GA4 Realtime's page-title dimension) ───────

async def _fetch_title_live(path: str, site: Site) -> str | None:
    base = site.url.rstrip("/")
    try:
        async with httpx.AsyncClient(timeout=6.0, follow_redirects=True) as client:
            resp = await client.get(
                f"{base}{path}", headers={"User-Agent": "WPCommandCenter/1.0 (title-lookup)"}
            )
            resp.raise_for_status()
        match = _TITLE_RE.search(resp.text)
        if match:
            return _html.unescape(re.sub(r"\s+", " ", match.group(1))).strip() or None
    except Exception as exc:
        logger.warning("Title resolution failed for %s%s: %s", base, path, exc)
    return None


async def _bulk_resolve_titles(paths: list[str], site: Site, db: AsyncSession) -> dict[str, str | None]:
    """Free lookup against already-crawled content (one query) first;
    bounded-concurrent live fetches — network only, no DB access, since
    AsyncSession isn't concurrency-safe — for whatever's left unmatched."""
    if not paths:
        return {}

    posts_r = await db.execute(
        select(ContentPost.url, ContentPost.title).where(ContentPost.site_id == site.id)
    )
    by_suffix: dict[str, str] = {}
    for url, title in posts_r.all():
        if title:
            by_suffix[urlparse(url).path.rstrip("/")] = title

    resolved: dict[str, str | None] = {}
    unmatched: list[str] = []
    for path in paths:
        known = by_suffix.get(path.rstrip("/"))
        if known:
            resolved[path] = known
        else:
            unmatched.append(path)

    sem = asyncio.Semaphore(8)

    async def _fetch(path: str) -> None:
        async with sem:
            resolved[path] = await _fetch_title_live(path, site)

    if unmatched:
        await asyncio.gather(*[_fetch(p) for p in unmatched])

    return resolved


# ── Schemas ─────────────────────────────────────────────────────────────────────

class AddUrlsRequest(BaseModel):
    site_id: str
    urls: list[str] = Field(min_length=1, max_length=_MAX_URLS_PER_REQUEST)


class WatchedUrlResponse(BaseModel):
    id: str
    url: str
    path: str
    title: str | None
    source: str
    created_at: datetime
    active_users: int
    # None whenever the range is "realtime" or GA isn't connected — the
    # Realtime API has no bounceRate/userEngagementDuration metrics, only
    # the standard reporting range does.
    avg_engagement_time: float | None = None
    bounce_rate: float | None = None

    model_config = {"from_attributes": True}


class AddUrlsResponse(BaseModel):
    added: list[WatchedUrlResponse]
    skipped_duplicate: list[str]
    invalid: list[dict[str, str]]


class WatchedUrlListResponse(BaseModel):
    items: list[WatchedUrlResponse]
    ga_connected: bool
    # The REAL calendar dates behind whatever `range` was requested — None
    # for "realtime" (no fixed date range). Lets the frontend label the
    # "Active Users" column and name exports after actual dates rather than
    # the preset key ("7d", "custom", ...).
    range_start: str | None = None
    range_end: str | None = None


class DailyActiveUsersItem(BaseModel):
    id: str
    url: str
    title: str | None
    daily: dict[str, int]


class DailyActiveUsersResponse(BaseModel):
    dates: list[str]
    items: list[DailyActiveUsersItem]


# ── Shared add logic (manual + CSV) ────────────────────────────────────────────

async def _add_urls(site: Site, raw_urls: list[str], source: str, db: AsyncSession) -> AddUrlsResponse:
    existing_r = await db.execute(select(WatchedUrl.path).where(WatchedUrl.site_id == site.id))
    existing_paths = {p for (p,) in existing_r.all()}

    to_add: list[tuple[str, str]] = []  # (full_url, path)
    skipped: list[str] = []
    invalid: list[dict[str, str]] = []
    seen_this_batch: set[str] = set()

    for raw in raw_urls:
        raw = raw.strip()
        if not raw:
            continue
        try:
            full_url, path = _normalize(raw, site.url)
        except ValueError as exc:
            invalid.append({"input": raw, "reason": str(exc)})
            continue
        if path in existing_paths or path in seen_this_batch:
            skipped.append(raw)
            continue
        seen_this_batch.add(path)
        to_add.append((full_url, path))

    titles = await _bulk_resolve_titles([path for _, path in to_add], site, db)

    added: list[WatchedUrl] = []
    now = datetime.now(timezone.utc)
    for full_url, path in to_add:
        title = titles.get(path)
        watched = WatchedUrl(
            site_id=site.id, url=full_url, path=path, title=title,
            title_resolved_at=now if title else None, source=source,
        )
        db.add(watched)
        added.append(watched)

    await db.flush()
    return AddUrlsResponse(
        added=[WatchedUrlResponse(**_serialize(w, 0)) for w in added],
        skipped_duplicate=skipped,
        invalid=invalid,
    )


def _serialize(
    w: WatchedUrl, active_users: int,
    avg_engagement_time: float | None = None, bounce_rate: float | None = None,
) -> dict[str, Any]:
    return {
        "id": w.id, "url": w.url, "path": w.path, "title": w.title,
        "source": w.source, "created_at": w.created_at, "active_users": active_users,
        "avg_engagement_time": avg_engagement_time, "bounce_rate": bounce_rate,
    }


async def _get_site_or_404(site_id: str, db: AsyncSession) -> Site:
    site = await db.get(Site, site_id)
    if not site:
        raise HTTPException(status_code=404, detail="Site not found")
    return site


def _resolve_date_range(range_key: str, start_date: str | None, end_date: str | None) -> tuple[str, str]:
    """Returns (start, end) as real YYYY-MM-DD calendar dates — "today" means
    the actual current date, not the keyword GA4 would also accept."""
    if range_key == "custom":
        if not (start_date and end_date and _DATE_RE.match(start_date) and _DATE_RE.match(end_date)):
            raise HTTPException(
                status_code=422, detail="Custom range requires start_date and end_date as YYYY-MM-DD"
            )
        return start_date, end_date

    if range_key not in _RANGE_KEYS:
        raise HTTPException(
            status_code=422,
            detail=f"Unknown range '{range_key}' — expected one of realtime, {', '.join(sorted(_RANGE_KEYS))}",
        )

    today = datetime.now(timezone.utc).date()
    if range_key == "today":
        start = today
    elif range_key == "yesterday":
        start = today - timedelta(days=1)
        today = start  # single-day range
    elif range_key == "7d":
        start = today - timedelta(days=6)   # 7 calendar days inclusive of today
    elif range_key == "28d":
        start = today - timedelta(days=27)
    elif range_key == "90d":
        start = today - timedelta(days=89)
    elif range_key == "qtd":
        start = _quarter_start(today)
    else:  # ytd
        start = date(today.year, 1, 1)

    return start.isoformat(), today.isoformat()


def _date_range_list(start_iso: str, end_iso: str) -> list[str]:
    """Every calendar date from start to end inclusive — used to fill in
    days a path had zero activity, since GA4 simply omits those rows."""
    start = date.fromisoformat(start_iso)
    end = date.fromisoformat(end_iso)
    return [(start + timedelta(days=i)).isoformat() for i in range((end - start).days + 1)]


def _path_variants(path: str) -> set[str]:
    """Both trailing-slash forms — GA4 records pagePath exactly as it was
    loaded, and we can't assume which form the site actually serves."""
    stripped = path.rstrip("/")
    return {path, stripped, f"{stripped}/"}


def _pick_variant(path: str, by_path: dict[str, Any]) -> Any | None:
    """Resolve one path's GA4 data out of a dict keyed by exact pagePath
    string, trying every trailing-slash variant.

    Every variant is queried (so data recorded under a differently-slashed
    form than the one stored isn't missed), but the results must never be
    SUMMED across variants: a stray hit under the "wrong" form (e.g. a
    request that bounced before a redirect completed) would silently
    inflate active-user counts, and for an already-averaged metric like
    bounce rate, adding two variants' rates together can push the result
    past 100% — which is exactly the bug this fixes. The canonical stored
    path wins whenever GA4 has any data for it; a different variant is only
    used as a fallback when the canonical one has none at all.
    """
    if path in by_path:
        return by_path[path]
    for variant in sorted(_path_variants(path)):
        if variant in by_path:
            return by_path[variant]
    return None


# ── Routes ──────────────────────────────────────────────────────────────────────

@router.post("", response_model=AddUrlsResponse, status_code=201, dependencies=[Depends(job_limiter)])
async def add_watched_urls(payload: AddUrlsRequest, db: AsyncSession = Depends(get_db)) -> AddUrlsResponse:
    site = await _get_site_or_404(payload.site_id, db)
    result = await _add_urls(site, payload.urls, source="manual", db=db)
    await db.commit()
    return result


@router.post("/csv", response_model=AddUrlsResponse, status_code=201, dependencies=[Depends(job_limiter)])
async def add_watched_urls_csv(
    site_id: str, file: UploadFile = File(...), db: AsyncSession = Depends(get_db),
) -> AddUrlsResponse:
    site = await _get_site_or_404(site_id, db)

    content = await file.read()
    if len(content) > _MAX_CSV_BYTES:
        raise HTTPException(status_code=413, detail="CSV file is too large (max 2MB)")

    urls = _parse_csv_urls(content)
    if not urls:
        raise HTTPException(status_code=422, detail="No URLs found — put one URL per row in the first column")
    if len(urls) > _MAX_URLS_PER_REQUEST:
        raise HTTPException(status_code=422, detail=f"Too many rows — max {_MAX_URLS_PER_REQUEST} per upload")

    result = await _add_urls(site, urls, source="csv", db=db)
    await db.commit()
    return result


@router.get("", response_model=WatchedUrlListResponse)
async def list_watched_urls(
    site_id: str,
    range: str = "realtime",
    start_date: str | None = None,
    end_date: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> WatchedUrlListResponse:
    """`range` mirrors GA4's own date-range picker: "realtime" (Active Users
    right now) or a preset/custom date range — see _RANGE_KEYS. Realtime can
    only be matched by page title (GA4 API limitation); every other range
    matches directly by path, which is more precise.
    """
    site = await _get_site_or_404(site_id, db)

    rows_r = await db.execute(
        select(WatchedUrl).where(WatchedUrl.site_id == site_id).order_by(WatchedUrl.created_at.desc())
    )
    rows = rows_r.scalars().all()

    # Self-heal any rows whose title never resolved (e.g. the live fetch
    # failed transiently at add-time) so they eventually start matching —
    # still needed for the "realtime" option even though other ranges
    # match by path now.
    missing = [w.path for w in rows if not w.title]
    if missing:
        resolved = await _bulk_resolve_titles(missing, site, db)
        now = datetime.now(timezone.utc)
        for w in rows:
            if not w.title and resolved.get(w.path):
                w.title = resolved[w.path]
                w.title_resolved_at = now
        await db.commit()

    # Resolved up front (and regardless of GA connection state) so the real
    # dates are always available to label the column / name an export, and
    # so a bad custom range fails fast rather than only once GA is reached.
    range_start: str | None = None
    range_end: str | None = None
    if range != "realtime":
        range_start, range_end = _resolve_date_range(range, start_date, end_date)

    cfg = (await db.execute(select(SiteConfig).where(SiteConfig.site_id == site_id))).scalar_one_or_none()
    ga_connected = False
    active_by_title: dict[str, int] = {}
    active_by_path: dict[str, int] = {}
    engagement_by_path: dict[str, dict[str, float]] = {}

    if cfg and cfg.ga_property_id:
        from app.api.auth import get_google_token
        from app.connectors.analytics import AnalyticsConnector

        token = await get_google_token(db)
        if token:
            ga_connected = True
            try:
                ga = AnalyticsConnector(token.access_token)
                if range == "realtime":
                    active_by_title = await ga.get_realtime_active_users_by_title(cfg.ga_property_id)
                else:
                    variants = sorted({v for w in rows for v in _path_variants(w.path)})
                    active_by_path = await ga.get_active_users_by_path(
                        cfg.ga_property_id, variants, range_start, range_end
                    )
                    # Realtime has no bounceRate/userEngagementDuration
                    # metrics — this is only ever fetched for a fixed range.
                    engagement_by_path = await ga.get_engagement_metrics_by_path(
                        cfg.ga_property_id, variants, range_start, range_end
                    )
            except HTTPException:
                raise
            except Exception as exc:
                logger.warning("GA4 fetch failed for site %s (range=%s): %s", site_id, range, exc)

    def _active_users(w: WatchedUrl) -> int:
        if range == "realtime":
            return active_by_title.get(w.title, 0) if w.title else 0
        return _pick_variant(w.path, active_by_path) or 0

    def _engagement(w: WatchedUrl) -> tuple[float | None, float | None]:
        if range == "realtime":
            return None, None
        metrics = _pick_variant(w.path, engagement_by_path)
        if not metrics:
            return 0.0, 0.0
        return metrics["avg_engagement_time"], metrics["bounce_rate"]

    items = []
    for w in rows:
        avg_engagement_time, bounce_rate = _engagement(w)
        items.append(WatchedUrlResponse(**_serialize(w, _active_users(w), avg_engagement_time, bounce_rate)))

    return WatchedUrlListResponse(
        items=items, ga_connected=ga_connected, range_start=range_start, range_end=range_end,
    )


@router.get("/daily", response_model=DailyActiveUsersResponse)
async def get_daily_active_users(
    site_id: str,
    range: str = "7d",
    start_date: str | None = None,
    end_date: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> DailyActiveUsersResponse:
    """Day-by-day Active Users per watched URL — the data behind a "day-wise
    breakdown" export. "Realtime" has no calendar days to break down, so
    it isn't a valid range here (every other _RANGE_KEYS preset, plus
    custom, is)."""
    if range == "realtime":
        raise HTTPException(
            status_code=422, detail="Day-wise breakdown needs a date range, not realtime."
        )

    await _get_site_or_404(site_id, db)  # 404 if the site doesn't exist
    start, end = _resolve_date_range(range, start_date, end_date)
    all_dates = _date_range_list(start, end)

    rows_r = await db.execute(
        select(WatchedUrl).where(WatchedUrl.site_id == site_id).order_by(WatchedUrl.created_at.desc())
    )
    rows = rows_r.scalars().all()

    cfg = (await db.execute(select(SiteConfig).where(SiteConfig.site_id == site_id))).scalar_one_or_none()
    if not (cfg and cfg.ga_property_id):
        raise HTTPException(
            status_code=400, detail="Connect Google Analytics for this site to export active users."
        )

    from app.api.auth import get_google_token
    from app.connectors.analytics import AnalyticsConnector

    token = await get_google_token(db)
    if not token:
        raise HTTPException(
            status_code=400, detail="Connect Google Analytics for this site to export active users."
        )

    ga = AnalyticsConnector(token.access_token)
    variants = sorted({v for w in rows for v in _path_variants(w.path)})
    try:
        raw = await ga.get_daily_active_users_by_path(cfg.ga_property_id, variants, start, end)
    except Exception as exc:
        logger.warning("GA4 daily fetch failed for site %s (range=%s): %s", site_id, range, exc)
        raise HTTPException(status_code=502, detail="Could not fetch data from Google Analytics.") from exc

    items = []
    for w in rows:
        # Resolve to exactly one trailing-slash variant — see _pick_variant.
        daily_for_path = _pick_variant(w.path, raw) or {}
        items.append(DailyActiveUsersItem(
            id=w.id, url=w.url, title=w.title,
            daily={d: daily_for_path.get(d, 0) for d in all_dates},
        ))
    return DailyActiveUsersResponse(dates=all_dates, items=items)


@router.delete("/{watched_url_id}", status_code=204)
async def delete_watched_url(watched_url_id: str, db: AsyncSession = Depends(get_db)) -> None:
    watched = await db.get(WatchedUrl, watched_url_id)
    if not watched:
        raise HTTPException(status_code=404, detail="Watched URL not found")
    await db.delete(watched)
    await db.commit()
