"""WordPress REST API connector.

Collection fetches are designed for enterprise-scale sites (thousands of
posts/pages) with two complementary strategies:
  - `fetch_all()`     — full crawl, pages fetched CONCURRENTLY once the first
                         page's `X-WP-TotalPages` header reveals how many
                         requests are needed. Used when nothing is cached yet.
  - `fetch_since()`    — incremental crawl: results sorted by `modified` date
                         descending, paginated sequentially, and stopped the
                         moment a page's oldest item is no newer than the
                         caller's checkpoint. A stable site with a handful of
                         edits since the last sync costs ONE request, not a
                         full re-crawl.
Neither imposes a product-level item cap — only a generous safety ceiling to
stop a pathological/misconfigured site from looping forever.
"""
import asyncio
import logging
from datetime import UTC, datetime
from typing import Any

import httpx

from app.config import settings
from app.connectors.retry import request_with_retries

logger = logging.getLogger(__name__)

# Backstop only — no real marketing site approaches this. Existing purely to
# bound a runaway loop against a misbehaving or misconfigured target.
HARD_SAFETY_CEILING = 20_000
_PAGE_SIZE = 100
_CONCURRENCY = 6


def parse_wp_datetime(value: str | None) -> datetime | None:
    """Parse a WP `*_gmt` timestamp as UTC-aware (WP sends naive-but-GMT strings)."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class WordPressConnector:
    def __init__(self, base_url: str, api_key: str | None = None) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or ""
        self._client: httpx.AsyncClient | None = None

    @property
    def has_auth(self) -> bool:
        return bool(self.api_key)

    @property
    def client(self) -> httpx.AsyncClient:
        if self._client is None:
            kwargs: dict = {
                "timeout": 30.0,
                "headers": {"User-Agent": "WP-Command-Center/0.1"},
            }
            if self.has_auth:
                kwargs["auth"] = httpx.BasicAuth(settings.WP_API_USERNAME, self.api_key)
            self._client = httpx.AsyncClient(**kwargs)
        return self._client

    async def close(self) -> None:
        if self._client:
            await self._client.aclose()

    async def _get(self, url: str, params: dict | None = None) -> httpx.Response:
        return await request_with_retries(
            lambda: self.client.get(url, params=params),
            what=f"WP GET {url}",
        )

    POST_FIELDS = "id,title,link,content,date,date_gmt,modified,modified_gmt,featured_media,excerpt,yoast_head_json"
    PAGE_FIELDS = "id,title,link,content,date,date_gmt,modified,modified_gmt,featured_media,excerpt,yoast_head_json"

    def _collection_url(self, endpoint: str) -> str:
        return f"{self.base_url}/wp-json/wp/v2/{endpoint}"

    async def _fetch_page(
        self, endpoint: str, fields: str, page: int, per_page: int = _PAGE_SIZE,
        orderby: str | None = None, order: str | None = None,
    ) -> httpx.Response:
        params: dict[str, Any] = {
            "per_page": per_page, "page": page, "status": "publish", "_fields": fields,
        }
        if orderby:
            params["orderby"] = orderby
            params["order"] = order or "desc"
        return await self._get(self._collection_url(endpoint), params=params)

    async def fetch_all(
        self, endpoint: str, fields: str, hard_ceiling: int = HARD_SAFETY_CEILING,
    ) -> tuple[list[dict[str, Any]], int]:
        """Every published item, regardless of collection size.

        Returns (items, total_reported_by_wp). The first page reveals
        `X-WP-TotalPages` — remaining pages are then fetched concurrently
        (bounded), turning an enterprise site's crawl into a handful of
        parallel round trips instead of one-at-a-time pagination.
        """
        first = await self._fetch_page(endpoint, fields, page=1)
        first.raise_for_status()
        items: list[dict[str, Any]] = list(first.json())
        total = int(first.headers.get("X-WP-Total") or len(items))
        total_pages = int(first.headers.get("X-WP-TotalPages") or 1)

        if total_pages <= 1:
            return items[:hard_ceiling], total

        capped_pages = min(total_pages, max(1, hard_ceiling // _PAGE_SIZE))
        if capped_pages < total_pages:
            logger.warning(
                "WP %s at %s: %d pages exceeds the safety ceiling — truncating to %d pages (%d items)",
                endpoint, self.base_url, total_pages, capped_pages, capped_pages * _PAGE_SIZE,
            )

        semaphore = asyncio.Semaphore(_CONCURRENCY)

        async def _fetch(page_num: int) -> list[dict[str, Any]]:
            async with semaphore:
                resp = await self._fetch_page(endpoint, fields, page=page_num)
                if resp.status_code == 400:  # past the last page — treat as empty
                    return []
                resp.raise_for_status()
                return resp.json()

        remaining = await asyncio.gather(*[_fetch(p) for p in range(2, capped_pages + 1)])
        for batch in remaining:
            items.extend(batch)
        return items, total

    async def fetch_since(
        self, endpoint: str, fields: str, modified_after: datetime | None,
        hard_ceiling: int = HARD_SAFETY_CEILING,
    ) -> tuple[list[dict[str, Any]], int]:
        """Items modified after `modified_after`, newest first.

        `modified_after=None` means "no checkpoint yet" — falls back to a
        full crawl. Otherwise pages sequentially (order matters for the
        early-exit) and stops as soon as a page's OLDEST item is no newer
        than the checkpoint, since everything after it is even older.
        """
        if modified_after is None:
            return await self.fetch_all(endpoint, fields, hard_ceiling)

        items: list[dict[str, Any]] = []
        page = 1
        total = 0
        while len(items) < hard_ceiling:
            resp = await self._fetch_page(
                endpoint, fields, page=page, orderby="modified", order="desc",
            )
            if resp.status_code == 400 and page > 1:
                break
            resp.raise_for_status()
            if page == 1:
                total = int(resp.headers.get("X-WP-Total") or 0)
            batch = resp.json()
            if not batch:
                break

            reached_checkpoint = False
            for item in batch:
                item_modified = parse_wp_datetime(item.get("modified_gmt") or item.get("modified"))
                if item_modified is not None and item_modified <= modified_after:
                    reached_checkpoint = True
                    break
                items.append(item)

            if reached_checkpoint or len(batch) < _PAGE_SIZE:
                break
            page += 1

        return items, total

    async def fetch_live_ids(self, endpoint: str, hard_ceiling: int = HARD_SAFETY_CEILING) -> set[int]:
        """Every published item's WP id — cheap (`_fields=id`), used to detect
        deletions: an id we have locally that's missing here was removed/unpublished."""
        items, _ = await self.fetch_all(endpoint, "id", hard_ceiling)
        return {item["id"] for item in items if "id" in item}

    # ── Back-compat thin wrappers used by call sites that just want "everything" ──

    async def get_all_posts(self, hard_ceiling: int = HARD_SAFETY_CEILING) -> list[dict[str, Any]]:
        items, _ = await self.fetch_all("posts", self.POST_FIELDS, hard_ceiling)
        return items

    async def get_all_pages(self, hard_ceiling: int = HARD_SAFETY_CEILING) -> list[dict[str, Any]]:
        items, _ = await self.fetch_all("pages", self.PAGE_FIELDS, hard_ceiling)
        return items

    async def get_posts(self, per_page: int = 100, page: int = 1) -> list[dict[str, Any]]:
        """Single page — used for lightweight checks (e.g. connection test), not bulk crawling."""
        response = await self._fetch_page("posts", self.POST_FIELDS, page=page, per_page=per_page)
        response.raise_for_status()
        return response.json()

    async def get_post(self, wp_post_id: int, content_type: str = "post") -> dict[str, Any] | None:
        """Fetch a single post OR page by its WordPress ID. Returns None if
        not found.

        Posts and pages share one global ID sequence in WordPress core, but
        are served from two DIFFERENT REST endpoints (`/posts/` vs
        `/pages/`) — querying the wrong one 404s even for content that's
        very much alive on the site. Callers must pass the item's actual
        content_type; the default of "post" is a guess of last resort only.
        """
        endpoint = "pages" if content_type == "page" else "posts"
        response = await self._get(
            f"{self.base_url}/wp-json/wp/v2/{endpoint}/{wp_post_id}",
            params={"_fields": self.PAGE_FIELDS if content_type == "page" else self.POST_FIELDS},
        )
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()

    async def get_plugins(self) -> list[dict[str, Any]]:
        if not self.has_auth:
            raise PermissionError("Plugin list requires an Application Password")
        response = await self._get(f"{self.base_url}/wp-json/wp/v2/plugins")
        response.raise_for_status()
        return response.json()

    async def update_post_meta(self, post_id: int, meta_key: str, meta_value: str) -> dict[str, Any]:
        if not self.has_auth:
            raise PermissionError("Updating post meta requires an Application Password")
        response = await self.client.post(
            f"{self.base_url}/wp-json/wp/v2/posts/{post_id}",
            json={"meta": {meta_key: meta_value}},
        )
        response.raise_for_status()
        return response.json()

    async def get_site_info(self) -> dict[str, Any]:
        """
        Fetch public site metadata from the WP REST API root endpoint.
        Returns name, description (tagline), url, and namespace list.
        No authentication required.
        """
        try:
            response = await self.client.get(f"{self.base_url}/wp-json/", timeout=10.0)
            if response.status_code == 200:
                data = response.json()
                return {
                    "name": data.get("name", ""),
                    "description": data.get("description", ""),
                    "url": data.get("url", self.base_url),
                    "namespaces": data.get("namespaces", []),
                }
        except Exception:
            pass
        return {"name": "", "description": "", "url": self.base_url, "namespaces": []}

    async def get_site_health(self) -> dict[str, Any]:
        response = await self.client.get(f"{self.base_url}/wp-json/wp-site-health/v1/tests/authorization-header")
        if response.status_code == 200:
            return response.json()
        return {}
