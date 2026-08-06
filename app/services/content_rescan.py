"""Re-analysing a single page, and doing it to many.

The per-page work lives here rather than in the route so the single-post
button and the bulk action run exactly the same code. Two implementations of
"rescan" would drift, and the one nobody clicks is the one that rots.

Bulk is not a loop over the HTTP endpoint. Each rescan costs a WordPress
fetch, a live page fetch and an AI call, so N clicks means N of each against
someone's production site — which is what the rate limiter exists to stop.
Running the batch server-side lets concurrency be bounded once, deliberately,
instead of being whatever the browser happened to fire.
"""
import asyncio
import hashlib
import html as html_lib
import logging
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.engine import AsyncSessionLocal
from app.database.models import Alert, ContentPost, Site

logger = logging.getLogger(__name__)

STATUS_KEY = "optimizer.bulk_rescan"
MAX_BATCH = 100
# Deliberately low: every unit of concurrency is another simultaneous request
# against the customer's live site, plus an AI call.
CONCURRENCY = 3
AI_TIMEOUT_SECONDS = 30


class PostGone(Exception):
    """WordPress returned a definitive 404 — the page is not there any more."""

    def __init__(self, title: str) -> None:
        super().__init__(title)
        self.title = title


@dataclass
class BulkProgress:
    total: int = 0
    done: int = 0
    failed: int = 0
    removed: int = 0
    running: bool = False
    started_at: str | None = None
    finished_at: str | None = None
    # Bounded: this drives a status banner, not an audit log.
    failures: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


async def _drop_missing_post(db: AsyncSession, post: ContentPost) -> str:
    """Remove a page WordPress no longer has, and its content-health alerts.

    Incremental sync only sees what changed, so it can never observe a
    deletion. Without this the row would sit here failing every future
    rescan until the next full reconciliation, up to a week away.
    """
    title = post.title
    alerts = (await db.execute(
        select(Alert).where(
            Alert.site_id == post.site_id,
            Alert.agent == "optimizer",
            Alert.type == "content_health",
        )
    )).scalars().all()
    for alert in alerts:
        if (alert.metadata_ or {}).get("post_id") == post.id:
            await db.delete(alert)
    await db.delete(post)
    return title


async def rescan_post(db: AsyncSession, post: ContentPost, site: Site) -> dict[str, Any]:
    """Re-fetch one page from WordPress and re-run the analysis.

    Raises PostGone when WordPress no longer has it, ValueError when it was
    never linked to a WordPress ID, and RuntimeError when the site could not
    be reached — each of which the callers translate differently.
    """
    from app.agents.optimizer.content_scorer import (
        _analyze,
        _fetch_page_signals,
        _generate_ai_recommendation,
    )
    from app.connectors.wordpress import WordPressConnector

    if not post.wp_post_id:
        raise ValueError("Post has no WordPress ID — cannot rescan")

    wp = WordPressConnector(site.url, site.api_key)
    try:
        wp_post = await wp.get_post(post.wp_post_id, content_type=post.content_type)
    except Exception as exc:
        raise RuntimeError(f"WordPress connection failed: {exc}") from exc
    finally:
        await wp.close()

    if not wp_post:
        raise PostGone(await _drop_missing_post(db, post))

    post_url = wp_post.get("link") or post.url
    # One live-page fetch yields schema types AND the rendered H1 count (the
    # H1 usually lives in the theme template, outside content.rendered).
    signals = await _fetch_page_signals(post_url)

    health_score, issues, breakdown, word_count, reading_time = _analyze(
        wp_post, site.url,
        extra_schema_types=signals["schema_types"],
        live_h1_count=signals["h1_count"],
    )

    post.health_score = health_score
    post.issues = issues
    post.word_count = word_count
    post.reading_time_minutes = reading_time
    post.score_breakdown = breakdown
    post.last_analyzed_at = datetime.now(UTC)

    raw_title = wp_post.get("title", {})
    title_str = raw_title.get("rendered", "") if isinstance(raw_title, dict) else str(raw_title)
    title_str = html_lib.unescape(re.sub(r"<[^>]+>", "", title_str)).strip()

    try:
        ai_rec = await asyncio.wait_for(
            _generate_ai_recommendation(
                title_str, health_score, word_count, issues, breakdown,
                site_context=site.site_context or None,
            ),
            timeout=AI_TIMEOUT_SECONDS,
        )
    except Exception:
        ai_rec = None

    # None means generation failed, so existing text stands. "" means the page
    # is genuinely clean now, and stale advice — a since-fixed "add FAQPage
    # schema" — must be cleared rather than displayed forever.
    if ai_rec is not None:
        post.ai_recommendation = ai_rec or None
        raw_content = wp_post.get("content", {})
        content_text = raw_content.get("rendered", "") if isinstance(raw_content, dict) else ""
        post.ai_rec_hash = hashlib.sha256(
            f"{title_str}|{health_score}|{content_text}".encode()
        ).hexdigest()

    return {
        "id": post.id,
        "health_score": post.health_score,
        "word_count": post.word_count,
        "reading_time_minutes": post.reading_time_minutes,
        "issues": post.issues,
        "score_breakdown": post.score_breakdown,
    }


async def _save(progress: BulkProgress) -> None:
    from app.services.app_settings import set_json_setting

    async with AsyncSessionLocal() as db:
        await set_json_setting(db, STATUS_KEY, progress.as_dict())
        await db.commit()


async def read_progress() -> dict[str, Any]:
    from app.services.app_settings import get_json_setting

    async with AsyncSessionLocal() as db:
        return await get_json_setting(db, STATUS_KEY)


async def run_bulk_rescan(post_ids: list[str]) -> None:
    """Rescan a batch, reporting progress as it goes.

    Each page gets its own session and commit so one failure costs one page,
    not the batch — and so partial progress survives a timeout or restart
    rather than rolling back everything done so far.
    """
    progress = BulkProgress(
        total=len(post_ids), running=True, started_at=datetime.now(UTC).isoformat()
    )
    await _save(progress)

    semaphore = asyncio.Semaphore(CONCURRENCY)
    lock = asyncio.Lock()

    async def one(post_id: str) -> None:
        async with semaphore:
            async with AsyncSessionLocal() as db:
                try:
                    row = (await db.execute(
                        select(ContentPost, Site)
                        .join(Site, ContentPost.site_id == Site.id)
                        .where(ContentPost.id == post_id)
                    )).first()
                    if row is None:
                        raise LookupError("Post not found")
                    await rescan_post(db, row[0], row[1])
                    await db.commit()
                    outcome, message = "done", None
                except PostGone as gone:
                    await db.commit()  # the removal itself must persist
                    outcome, message = "removed", f"{gone.title}: no longer on WordPress"
                except Exception as exc:
                    await db.rollback()
                    outcome = "failed"
                    message = f"{post_id[:8]}: {exc}"
                    logger.warning("Bulk rescan failed for %s: %s", post_id, exc)

        async with lock:
            if outcome == "done":
                progress.done += 1
            elif outcome == "removed":
                progress.removed += 1
            else:
                progress.failed += 1
            if message and len(progress.failures) < 10:
                progress.failures.append(message)
            # Persist as it goes so the UI can show real movement rather than
            # a spinner that reveals nothing until the end.
            await _save(progress)

    await asyncio.gather(*(one(pid) for pid in post_ids))

    progress.running = False
    progress.finished_at = datetime.now(UTC).isoformat()
    await _save(progress)


__all__ = [
    "CONCURRENCY", "MAX_BATCH", "STATUS_KEY",
    "BulkProgress", "PostGone", "read_progress", "rescan_post", "run_bulk_rescan",
]
