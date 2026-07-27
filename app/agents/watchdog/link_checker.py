"""Link Checker — crawls posts/pages and detects broken links (internal + external).

Accuracy contract:
- Alert identity is the URL. Existing alerts are updated in place, so
  created_at and a dismissed/acknowledged status survive re-runs — a
  dismissed false positive never resurrects. Links that now resolve delete
  their alert. Teams notifications fire only for newly discovered links.
- Only URLs actually verified this run are reconciled; URLs beyond the
  per-run cap keep their existing alerts untouched (unverified ≠ fixed).

Scale: every published post/page is fetched (uncapped, concurrent pagination
via wp_fetch), and up to LINK_CHECK_MAX_URLS unique links are verified per
run, internal first. Coverage is logged so silent truncation can't
masquerade as a clean bill.
"""
import asyncio
import logging
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx
from sqlalchemy import delete, select

from app.agents.base import BaseAgent
from app.agents.wp_fetch import get_pages_cached, get_posts_cached
from app.config import settings
from app.database.models import Alert, Site

logger = logging.getLogger(__name__)

_SKIP_PREFIXES = ("#", "mailto:", "tel:", "javascript:", "data:", "ftp:", "file:")


class _LinkExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "a":
            for attr, val in attrs:
                if attr == "href" and val and not val.lower().startswith(_SKIP_PREFIXES):
                    self.links.append(val)


def _resolve_href(href: str, base_url: str) -> str | None:
    """Absolute http(s) URL with fragment stripped, or None if uncheckable."""
    href = href.strip()
    if not href:
        return None
    if href.startswith("//"):
        # Protocol-relative — inherits the page scheme, NOT a path on our site
        href = f"{urlparse(base_url).scheme}:{href}"
    elif href.startswith("/"):
        href = f"{base_url}{href}"
    elif not href.startswith(("http://", "https://")):
        href = urljoin(base_url + "/", href)
    parsed = urlparse(href)
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        return None
    return parsed._replace(fragment="").geturl()


# Browser-like headers — a bot UA gets 403/429 from Cloudflare, LinkedIn, etc.
_BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# Codes that mean "reachable" — auth walls, bot protection, rate limiting, or
# servers that simply dislike HEAD/automated requests. A real user's browser
# opens these fine, so they must NOT be reported as broken links.
_REACHABLE_STATUSES = frozenset({401, 403, 405, 406, 408, 409, 429, 451, 503, 999})


async def _get_status_streaming(client: httpx.AsyncClient, url: str) -> int:
    """GET status without downloading the body — headers are enough."""
    async with client.stream("GET", url, follow_redirects=True) as resp:
        return resp.status_code


async def _check_url(client: httpx.AsyncClient, url: str, retries: int = 2) -> int:
    """Return the final HTTP status code. 0 = unreachable after retries.

    Retries transient failures (network errors, 5xx) with backoff and falls
    back to GET whenever HEAD returns any error code, since many servers reject
    HEAD requests outright.
    """
    status = 0
    for attempt in range(retries + 1):
        try:
            resp = await client.head(url, follow_redirects=True)
            status = resp.status_code
            # HEAD is frequently unsupported — confirm anything >= 400 with a GET
            if status >= 400:
                try:
                    status = await _get_status_streaming(client, url)
                except (httpx.TimeoutException, httpx.HTTPError):
                    pass
            # A non-5xx answer is definitive — stop retrying
            if status < 500:
                return status
        except (httpx.TimeoutException, httpx.HTTPError):
            status = 0
        except Exception:
            status = 0

        if attempt < retries:
            await asyncio.sleep(0.6 * (attempt + 1))

    return status


def _classify(status: int, is_internal: bool) -> str | None:
    """Map a status to 'critical' / 'warning' / None (not broken)."""
    if status in _REACHABLE_STATUSES:
        return None
    if 200 <= status < 400:
        return None
    # Genuinely missing pages
    if status in (404, 410):
        return "critical"
    # Could not connect at all after retries
    if status == 0:
        return "critical" if is_internal else "warning"
    # Persistent server errors (5xx other than 503)
    if 500 <= status < 600:
        return "warning"
    # Any other 4xx we couldn't explain away
    if 400 <= status < 500:
        return "warning"
    return None


def _plan_reconcile(
    existing: set[str], broken: set[str], checked: set[str]
) -> tuple[set[str], set[str], set[str]]:
    """(to_create, to_update, to_delete) by URL.

    Unchecked URLs are untouched — a link we didn't verify is not "fixed".
    """
    to_create = broken - existing
    to_update = broken & existing
    to_delete = (existing & checked) - broken
    return to_create, to_update, to_delete


class LinkChecker(BaseAgent):
    async def run(self, site_id: str) -> list[Alert]:
        result = await self.db.execute(select(Site).where(Site.id == site_id))
        site = result.scalar_one_or_none()
        if not site:
            return []

        try:
            posts = await get_posts_cached(site.url, site.api_key)
            pages = await get_pages_cached(site.url, site.api_key)
        except Exception as exc:
            # Silent [] would render as "all links healthy" — fail loudly instead.
            raise RuntimeError(f"Could not fetch content from {site.url}: {exc}") from exc

        site_domain = urlparse(site.url).netloc
        base_url = site.url.rstrip("/")

        # Map url -> set of pages it was found on
        links_map: dict[str, set[str]] = {}
        for item in posts + pages:
            content_raw = item.get("content", {})
            html = content_raw.get("rendered", "") if isinstance(content_raw, dict) else ""
            found_on = item.get("link", "")

            parser = _LinkExtractor()
            parser.feed(html)
            for href in parser.links:
                clean = _resolve_href(href, base_url)
                if clean is None:
                    continue
                links_map.setdefault(clean, set())
                if found_on:
                    links_map[clean].add(found_on)

        internal = {u: fo for u, fo in links_map.items() if urlparse(u).netloc == site_domain}
        external = {u: fo for u, fo in links_map.items() if u not in internal}

        # Deterministic order, internal first, bounded per run
        cap = settings.LINK_CHECK_MAX_URLS
        to_check: list[tuple[str, set[str], bool]] = (
            [(u, internal[u], True) for u in sorted(internal)]
            + [(u, external[u], False) for u in sorted(external)]
        )[:cap]
        logger.info(
            "LinkChecker %s: verifying %d/%d unique links (%d internal, %d external) from %d documents",
            site_domain, len(to_check), len(links_map), len(internal), len(external), len(posts) + len(pages),
        )

        broken: dict[str, tuple[int, list[str], bool, str]] = {}  # url -> (status, found_on, is_internal, severity)
        semaphore = asyncio.Semaphore(settings.LINK_CHECK_CONCURRENCY)

        async def check(client: httpx.AsyncClient, url: str, found_on: set[str], is_internal: bool) -> None:
            async with semaphore:
                status = await _check_url(client, url)
                severity = _classify(status, is_internal)
                if severity is not None:
                    broken[url] = (status, sorted(found_on)[:10], is_internal, severity)

        async with httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, connect=5.0),
            headers=_BROWSER_HEADERS,
            limits=httpx.Limits(
                max_connections=settings.LINK_CHECK_CONCURRENCY,
                max_keepalive_connections=8,
            ),
        ) as client:
            await asyncio.gather(*[check(client, u, fo, i) for u, fo, i in to_check])

        # ── Reconcile against existing alerts (keyed by URL) ─────────────────
        existing_r = await self.db.execute(
            select(Alert).where(
                Alert.site_id == site_id,
                Alert.agent == "watchdog",
                Alert.type == "broken_link",
            )
        )
        existing_by_url: dict[str, list[Alert]] = {}
        for a in existing_r.scalars().all():
            url = (a.metadata_ or {}).get("url")
            if url:
                existing_by_url.setdefault(url, []).append(a)

        checked_urls = {u for u, _, _ in to_check}
        to_create, to_update, to_delete = _plan_reconcile(
            set(existing_by_url), set(broken), checked_urls
        )

        stale_ids = [a.id for u in to_delete for a in existing_by_url[u]]
        # Legacy duplicates for a still-broken URL: keep the first, drop the rest
        stale_ids += [a.id for u in to_update for a in existing_by_url[u][1:]]
        if stale_ids:
            await self.db.execute(delete(Alert).where(Alert.id.in_(stale_ids)))

        def _fields(url: str) -> tuple[str, str, dict]:
            status_code, found_on, is_internal, _ = broken[url]
            label = f"HTTP {status_code}" if status_code else "Connection failed / Timeout"
            link_type = "Internal" if is_internal else "External"
            title = f"{link_type} broken link ({label}): {url[:70]}"
            description = f"Found on: {found_on[0] if found_on else 'unknown'}"
            meta = {
                "url": url,
                "status_code": status_code,
                "found_on": found_on,
                "found_on_count": len(links_map.get(url, [])),
                "is_internal": is_internal,
            }
            return title, description, meta

        for url in to_update:
            alert = existing_by_url[url][0]
            alert.title, alert.description, alert.metadata_ = _fields(url)
            alert.severity = broken[url][3]
            # status (open/acknowledged/dismissed) and created_at survive on purpose

        alerts: list[Alert] = []
        for url in sorted(to_create):
            title, description, meta = _fields(url)
            alert = await self.create_alert(
                site_id=site_id,
                agent="watchdog",
                severity=broken[url][3],
                type_="broken_link",
                title=title,
                description=description,
                metadata=meta,
            )
            alerts.append(alert)

        logger.info(
            "LinkChecker %s: %d broken (%d new, %d updated, %d fixed/removed)",
            site_domain, len(broken), len(to_create), len(to_update), len(to_delete),
        )
        return alerts
