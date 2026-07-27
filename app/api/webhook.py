"""Inbound webhook receiver for the WP Command Center WordPress plugin.

The plugin POSTs signed JSON events (post.published, post.updated,
plugins.updated). Authentication is the HMAC signature — computed over the
raw request body with the site's shared `webhook_secret` — plus a replay
window on the embedded timestamp. This endpoint is deliberately outside the
JWT auth wall: WordPress cannot hold a dashboard login.
"""
import hashlib
import hmac
import json
import logging
import time
from typing import Any

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.engine import AsyncSessionLocal, get_db
from app.database.models import Site
from app.security.rate_limit import rate_limit

logger = logging.getLogger(__name__)
router = APIRouter()

REPLAY_WINDOW_SECONDS = 300
webhook_limiter = rate_limit("webhook", times=60, seconds=60)


def _norm_url(url: str) -> str:
    return url.rstrip("/").lower()


async def _sync_site_content(site_id: str) -> None:
    """Refresh content_posts after a post event, with a fresh session."""
    from app.agents.wp_fetch import invalidate
    from app.api.sites import _do_sync

    async with AsyncSessionLocal() as db:
        try:
            site = (await db.execute(select(Site).where(Site.id == site_id))).scalar_one_or_none()
            if not site:
                return
            invalidate(site.url)
            await _do_sync(site, db)
            await db.commit()
            logger.info("Webhook-triggered content sync completed for site %s", site_id)
        except Exception as exc:
            await db.rollback()
            logger.error("Webhook-triggered sync failed for site %s: %s", site_id, exc)


async def _audit_site_plugins(site_id: str) -> None:
    """Re-run the plugin auditor after a plugins.updated event."""
    from app.agents.watchdog.plugin_audit import PluginAuditor

    async with AsyncSessionLocal() as db:
        try:
            await PluginAuditor(db).run(site_id)
            await db.commit()
            logger.info("Webhook-triggered plugin audit completed for site %s", site_id)
        except Exception as exc:
            await db.rollback()
            logger.error("Webhook-triggered plugin audit failed for site %s: %s", site_id, exc)


@router.post("", dependencies=[Depends(webhook_limiter)])
async def receive_webhook(
    request: Request,
    background_tasks: BackgroundTasks,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    raw_body = await request.body()
    signature = request.headers.get("X-WPCC-Signature", "")

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON body")

    event = payload.get("event", "")
    site_url = payload.get("site_url", "")
    ts = payload.get("ts", 0)

    if not event or not site_url:
        raise HTTPException(status_code=400, detail="Missing event or site_url")

    # Replay window
    if not isinstance(ts, (int, float)) or abs(time.time() - ts) > REPLAY_WINDOW_SECONDS:
        raise HTTPException(status_code=401, detail="Timestamp outside allowed window")

    # Find the site by URL
    sites = (await db.execute(select(Site))).scalars().all()
    site = next((s for s in sites if _norm_url(s.url) == _norm_url(site_url)), None)
    if not site:
        raise HTTPException(status_code=404, detail="Unknown site")
    if not site.webhook_secret:
        raise HTTPException(status_code=401, detail="Webhook secret not configured for this site")

    # Verify HMAC over the RAW body — must match the plugin's signing exactly
    expected = "sha256=" + hmac.new(
        site.webhook_secret.encode(), raw_body, hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(expected, signature):
        logger.warning("Webhook signature mismatch from %s", site_url)
        raise HTTPException(status_code=401, detail="Invalid signature")

    # Dispatch
    if event in ("post.published", "post.updated"):
        background_tasks.add_task(_sync_site_content, site.id)
    elif event == "plugins.updated":
        background_tasks.add_task(_audit_site_plugins, site.id)
    else:
        logger.info("Webhook event '%s' from %s acknowledged (no handler)", event, site_url)

    return {"status": "accepted", "event": event}
