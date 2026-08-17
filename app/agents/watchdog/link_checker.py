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
import hashlib
import logging
import uuid
from datetime import UTC, datetime
from html.parser import HTMLParser
from urllib.parse import urljoin, urlparse

import httpx
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app.agents.base import BaseAgent
from app.agents.wp_fetch import get_pages_cached, get_posts_cached
from app.config import settings
from app.database.models import Alert, LinkCheck, Site

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


def _resolve_href(href: str, base_url: str, page_url: str | None = None) -> str | None:
    """Absolute http(s) URL with fragment stripped, or None if uncheckable.

    `base_url` is the site root (for root-relative hrefs); `page_url` is the
    document the href was found on, which is what a *document*-relative href
    like "../pricing/" or "setup" actually resolves against per the HTML spec.
    Resolving those against the site root instead produced confident 404s for
    links that work perfectly in a browser.
    """
    href = href.strip()
    if not href:
        return None
    if href.startswith("//"):
        # Protocol-relative — inherits the page scheme, NOT a path on our site
        href = f"{urlparse(base_url).scheme}:{href}"
    elif href.startswith("/"):
        href = f"{base_url}{href}"
    elif not href.startswith(("http://", "https://")):
        # A directory-style page URL ("/blog/post/") is its own base; anything
        # else falls back to the site root, which is the old behaviour and the
        # right answer when we don't know the containing document.
        anchor = page_url if page_url and page_url.startswith(("http://", "https://")) else base_url + "/"
        href = urljoin(anchor, href)
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

# ...except on your own domain. An internal page answering 401/403 is not a
# bot-protection quirk, it is a page your readers cannot open — a lost login
# wall or a post flipped back to private. External hosts keep the benefit of
# the doubt, because there a 403 usually is just Cloudflare disliking us.
_INTERNAL_BROKEN_STATUSES = frozenset({401, 403})


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


# Everything legal in a hostname, plus the port separator and IPv6 brackets.
_HOST_ALLOWED = set("abcdefghijklmnopqrstuvwxyz0123456789-._:[]")


def is_malformed_host(url: str) -> bool:
    """True when the URL's host could never resolve for anyone.

    The case this exists for is an `href` containing prose — someone pasting a
    sentence into WordPress's link field, which yields
    `http://A%20manual%20approach%20may%20work...` as a link on the live page.

    Without this it is indistinguishable from an external host that happened
    not to answer, and gets the benefit of the doubt reserved for third-party
    flakiness: reported as a warning that might come back on its own. It never
    will. It is a content defect on your own page, and it is certain.
    """
    host = urlparse(url).netloc.lower()
    if not host:
        return True
    return any(c not in _HOST_ALLOWED for c in host)


def _classify(status: int, is_internal: bool) -> str | None:
    """Map a status to 'critical' / 'warning' / None (not broken)."""
    if is_internal and status in _INTERNAL_BROKEN_STATUSES:
        return "warning"
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


def plan_link_batch(
    internal: list[str],
    external: list[str],
    last_checked: dict[str, datetime],
    *,
    budget: int,
    external_share: float = 0.4,
) -> list[tuple[str, bool]]:
    """Which links to verify this run, least-recently-checked first.

    Two separate defects lived in the line this replaces —
    `(internal_sorted + external_sorted)[:cap]`.

    The first is starvation. Concatenating the classes before slicing means a
    site with more internal links than the whole budget never checks a single
    external one. On the install this was found on that was 1,294 internal
    links against a budget of 500: every external link on the site was
    unverified, permanently. So each class gets a guaranteed share, and
    whatever one class cannot use flows to the other rather than being wasted.

    The second is that a sorted slice is the *same* slice every run. Coverage
    has to come from rotation, exactly as it does for the performance sweep:
    least-recently-checked first, never-checked ahead of everything, so
    repeated runs reach new links instead of re-verifying one alphabetical
    window forever.
    """
    budget = max(0, budget)
    far_past = datetime.min.replace(tzinfo=UTC)

    def stalest(urls: list[str]) -> list[str]:
        # Sorted by url as the tie-break so a run is deterministic when many
        # links share a timestamp — which they do, since a run stamps them all.
        return sorted(dict.fromkeys(urls), key=lambda u: (last_checked.get(u) or far_past, u))

    ranked_internal, ranked_external = stalest(internal), stalest(external)

    want_external = min(len(ranked_external), int(budget * external_share))
    want_internal = min(len(ranked_internal), budget - want_external)
    # Hand back anything the other class left on the table.
    want_external = min(len(ranked_external), budget - want_internal)

    return (
        [(u, True) for u in ranked_internal[:want_internal]]
        + [(u, False) for u in ranked_external[:want_external]]
    )


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
    async def _record_checks(
        self, site_id: str, outcomes: dict[str, tuple[int, bool]]
    ) -> None:
        """Stamp every link verified this run, so the next one moves on.

        An upsert rather than delete-and-insert: a link that disappears from
        the site keeps its row until the sweep below removes it, and a row that
        survives keeps its identity.
        """
        if not outcomes:
            return
        now = datetime.now(UTC)
        rows = [
            {
                "id": str(uuid.uuid4()),
                "site_id": site_id,
                "url_hash": hashlib.sha256(url.encode()).hexdigest(),
                "url": url,
                "is_internal": is_internal,
                "status": status,
                "checked_at": now,
            }
            for url, (status, is_internal) in outcomes.items()
        ]
        stmt = pg_insert(LinkCheck).values(rows)
        await self.db.execute(
            stmt.on_conflict_do_update(
                index_elements=[LinkCheck.site_id, LinkCheck.url_hash],
                set_={
                    "status": stmt.excluded.status,
                    "checked_at": stmt.excluded.checked_at,
                    "is_internal": stmt.excluded.is_internal,
                },
            )
        )

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
                clean = _resolve_href(href, base_url, found_on or None)
                if clean is None:
                    continue
                links_map.setdefault(clean, set())
                if found_on:
                    links_map[clean].add(found_on)

        internal = {u: fo for u, fo in links_map.items() if urlparse(u).netloc == site_domain}
        external = {u: fo for u, fo in links_map.items() if u not in internal}

        # When each link was last verified — the memory that makes the budget
        # rotate instead of re-checking one alphabetical window forever.
        seen_rows = (await self.db.execute(
            select(LinkCheck.url, LinkCheck.checked_at).where(LinkCheck.site_id == site_id)
        )).all()
        last_checked = dict(seen_rows)

        cap = settings.LINK_CHECK_MAX_URLS
        planned = plan_link_batch(
            list(internal), list(external), last_checked,
            budget=cap, external_share=settings.LINK_CHECK_EXTERNAL_SHARE,
        )
        to_check: list[tuple[str, set[str], bool]] = [
            (u, (internal if is_int else external)[u], is_int) for u, is_int in planned
        ]
        never = sum(1 for u in links_map if u not in last_checked)
        logger.info(
            "LinkChecker %s: verifying %d/%d unique links (%d internal, %d external) "
            "from %d documents — %d of %d links have never been checked",
            site_domain, len(to_check), len(links_map),
            sum(1 for _, i in planned if i), sum(1 for _, i in planned if not i),
            len(posts) + len(pages), never, len(links_map),
        )

        broken: dict[str, tuple[int, list[str], bool, str]] = {}  # url -> (status, found_on, is_internal, severity)
        outcomes: dict[str, tuple[int, bool]] = {}   # url -> (status, is_internal)
        semaphore = asyncio.Semaphore(settings.LINK_CHECK_CONCURRENCY)

        async def check(client: httpx.AsyncClient, url: str, found_on: set[str], is_internal: bool) -> None:
            async with semaphore:
                if is_malformed_host(url):
                    # No request to make — this host cannot exist. Skipping the
                    # HTTP attempt also stops a page of prose being retried
                    # three times before reaching the same conclusion.
                    outcomes[url] = (0, is_internal)
                    broken[url] = (0, sorted(found_on)[:10], is_internal, "critical")
                    return
                status = await _check_url(client, url)
                # Recorded whatever the verdict — the rotation needs to know a
                # link was looked at, not just that it was found wanting.
                outcomes[url] = (status, is_internal)
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

        await self._record_checks(site_id, outcomes)

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
            malformed = is_malformed_host(url)
            where = found_on[0] if found_on else "unknown"

            if malformed:
                # Naming the cause, because "connection failed" sends someone
                # to check a server when the fix is to edit the link text.
                title = f"Malformed link on the page: {url[:70]}"
                description = (
                    f"The href is not a URL — its host is {urlparse(url).netloc[:60]!r}. "
                    "This usually means text was pasted into the link field. "
                    f"Edit the link on: {where}"
                )
            else:
                label = f"HTTP {status_code}" if status_code else "Connection failed / Timeout"
                link_type = "Internal" if is_internal else "External"
                title = f"{link_type} broken link ({label}): {url[:70]}"
                description = f"Found on: {where}"

            meta = {
                "url": url,
                "status_code": status_code,
                "found_on": found_on,
                "found_on_count": len(links_map.get(url, [])),
                "is_internal": is_internal,
                "malformed": malformed,
            }
            return title, description, meta

        for url in to_update:
            title, description, meta = _fields(url)
            # update_alert notifies if this just escalated (e.g. 500 -> 404);
            # status (open/acknowledged/dismissed) and created_at survive.
            await self.update_alert(
                existing_by_url[url][0],
                severity=broken[url][3], title=title,
                description=description, metadata=meta,
            )

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
