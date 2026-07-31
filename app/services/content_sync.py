"""Incremental full-site content sync.

Mirrors WordPress posts AND pages into `content_posts` (content_type
distinguishes them) — the single source of truth every optimizer/watchdog
agent matches against. Two modes, chosen automatically per collection:

  - incremental: pulls only items modified since the site's stored
    checkpoint (`{posts,pages}_synced_through`). A stable site costs one
    cheap request per collection instead of a full re-crawl.
  - full reconciliation: pulls everything and diffs against the live WP ID
    set to prune posts/pages that were deleted or unpublished — incremental
    sync alone can never detect a removal, since a missing item just doesn't
    appear in "what changed" at all.

Reconciliation runs when: there's no checkpoint yet (first sync for this
site/collection), the live item count has dropped since we last checked
(cheap early signal — something disappeared), or RECONCILE_INTERVAL has
elapsed since the last one (catches anything the count check missed, e.g. a
simultaneous delete+publish that leaves the count unchanged).
"""
import hashlib
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.connectors.wordpress import WordPressConnector, parse_wp_datetime
from app.database.models import ContentPost, Site

logger = logging.getLogger(__name__)

RECONCILE_INTERVAL = timedelta(days=7)


def _strip_html(value: str) -> str:
    return re.sub(r"<[^>]+>", "", value).strip()


def _title_of(item: dict[str, Any]) -> str:
    raw = item.get("title", {})
    title = raw.get("rendered", "") if isinstance(raw, dict) else str(raw)
    return _strip_html(title)


def _content_of(item: dict[str, Any]) -> str:
    raw = item.get("content", {})
    return raw.get("rendered", "") if isinstance(raw, dict) else ""


async def _sync_collection(
    db: AsyncSession,
    site: Site,
    wp: WordPressConnector,
    *,
    content_type: str,
    endpoint: str,
    fields: str,
    checkpoint: datetime | None,
    force_reconcile: bool,
) -> dict[str, Any]:
    """Sync one collection (posts or pages) and return a summary + new checkpoint."""
    existing_r = await db.execute(
        select(ContentPost).where(
            ContentPost.site_id == site.id, ContentPost.content_type == content_type
        )
    )
    existing_by_wp_id = {p.wp_post_id: p for p in existing_r.scalars().all()}

    do_full = force_reconcile or checkpoint is None
    items, remote_total = await wp.fetch_since(endpoint, fields, None if do_full else checkpoint)

    # A cheap early signal that something was removed even when we weren't
    # due for a full reconciliation — trust it over the interval timer.
    if not do_full and remote_total and remote_total < len(existing_by_wp_id):
        do_full = True
        items, remote_total = await wp.fetch_since(endpoint, fields, None)

    upserted = 0
    newest_seen = checkpoint
    for item in items:
        wp_id = item.get("id")
        if not wp_id:
            continue
        modified_at = parse_wp_datetime(item.get("modified_gmt") or item.get("modified"))
        if modified_at and (newest_seen is None or modified_at > newest_seen):
            newest_seen = modified_at

        title = _title_of(item)
        content_text = _content_of(item)
        content_hash = hashlib.sha256(content_text.encode()).hexdigest()
        url = item.get("link", "")

        post = existing_by_wp_id.get(wp_id)
        if post:
            post.title = title
            post.url = url
            post.content_hash = content_hash
            # wp_modified_at is intentionally NOT touched here — it's the
            # "last fully analyzed as of" watermark the scoring agents own;
            # the sync mirror and the analysis watermark are different
            # concerns even though they both track WP's modified date.
        else:
            post = ContentPost(
                site_id=site.id,
                wp_post_id=wp_id,
                content_type=content_type,
                title=title,
                url=url,
                content_hash=content_hash,
                health_score=50,
                traffic_30d=0,
                traffic_trend=[],
                issues=[],
            )
            db.add(post)
            existing_by_wp_id[wp_id] = post
        upserted += 1

    deleted = 0
    if do_full:
        live_ids = {item.get("id") for item in items if item.get("id")}
        stale_ids = [
            post.id for wp_id, post in existing_by_wp_id.items() if wp_id not in live_ids
        ]
        if stale_ids:
            from sqlalchemy import delete as sa_delete
            await db.execute(sa_delete(ContentPost).where(ContentPost.id.in_(stale_ids)))
            deleted = len(stale_ids)

    return {
        "synced": upserted,
        "deleted": deleted,
        "checkpoint": newest_seen,
        "full": do_full,
    }


async def sync_site_content(site: Site, db: AsyncSession) -> dict[str, Any]:
    """Pull posts + pages from WordPress and upsert into content_posts.

    No item-count cap — every published post and page is mirrored. Cost is
    kept down by only asking WordPress for what changed since the last sync,
    except during the periodic full reconciliation pass that also catches
    deletions.
    """
    wp = WordPressConnector(site.url, site.api_key)
    now = datetime.now(UTC)
    reconcile_due = (
        site.last_full_reconciled_at is None
        or now - site.last_full_reconciled_at > RECONCILE_INTERVAL
    )

    try:
        posts_result = await _sync_collection(
            db, site, wp,
            content_type="post", endpoint="posts", fields=WordPressConnector.POST_FIELDS,
            checkpoint=site.posts_synced_through, force_reconcile=reconcile_due,
        )
        pages_result = await _sync_collection(
            db, site, wp,
            content_type="page", endpoint="pages", fields=WordPressConnector.PAGE_FIELDS,
            checkpoint=site.pages_synced_through, force_reconcile=reconcile_due,
        )
        site.status = "active"
    except Exception as exc:
        site.status = "error"
        logger.warning("Content sync failed for %s: %s", site.url, exc)
        raise
    finally:
        await wp.close()

    site.posts_synced_through = posts_result["checkpoint"]
    site.pages_synced_through = pages_result["checkpoint"]
    if posts_result["full"] or pages_result["full"]:
        site.last_full_reconciled_at = now
    site.last_synced_at = now

    await db.flush()

    mode = "full" if (posts_result["full"] or pages_result["full"]) else "incremental"
    logger.info(
        "Content sync for %s (%s): %d post(s), %d page(s) synced, %d removed",
        site.url, mode, posts_result["synced"], pages_result["synced"],
        posts_result["deleted"] + pages_result["deleted"],
    )
    return {
        "posts_synced": posts_result["synced"],
        "pages_synced": pages_result["synced"],
        "removed": posts_result["deleted"] + pages_result["deleted"],
        "mode": mode,
    }
