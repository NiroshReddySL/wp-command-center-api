"""How long a stored report lives.

Reports accumulate — one per site per generate, and nobody reads the ninth
oldest. But a report is a document that may already have been sent to someone,
so "exceeded a count" is not grounds to destroy it. Deletion is therefore two
stages: retention moves an item to a trash, and only time or an explicit
instruction removes it for good.

Three rules, and the interesting part of each is what it refuses to do:

- Only the newest few reports per site stay active. Locked ones are invisible
  to this rule — not "counted but skipped", but excluded from the count, so
  locking three reports does not stop new ones from having room.
- Trash empties after 30 days. Locked items cannot be in the trash, so the
  purge can never reach one.
- Restoring locks. Without that, restoring a report that already has three
  newer siblings would put it straight back in the trash on the next generate,
  and the restore button would look broken.
"""
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import Select, delete, false, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import ReviewItem

logger = logging.getLogger(__name__)

# Active reports kept per site. Older ones are trashed, not deleted.
KEEP_LATEST = 3
# How long a trashed report can still be restored.
TRASH_TTL_DAYS = 30


def active(action_type: str, site_id: str | None = None) -> Select:
    """Reports that are not in the trash."""
    query = select(ReviewItem).where(
        ReviewItem.action_type == action_type,
        ReviewItem.trashed_at.is_(None),
    )
    if site_id:
        query = query.where(ReviewItem.site_id == site_id)
    return query


def trashed(action_type: str, site_id: str | None = None) -> Select:
    query = select(ReviewItem).where(
        ReviewItem.action_type == action_type,
        ReviewItem.trashed_at.isnot(None),
    )
    if site_id:
        query = query.where(ReviewItem.site_id == site_id)
    return query


def expires_at(item: ReviewItem) -> datetime | None:
    """When the trash will drop this item, or None if it is not in the trash."""
    if item.trashed_at is None:
        return None
    return item.trashed_at + timedelta(days=TRASH_TTL_DAYS)


async def apply_retention(
    db: AsyncSession, action_type: str, site_id: str, *, keep: int = KEEP_LATEST
) -> list[str]:
    """Trash every unlocked report beyond the newest `keep` for this site.

    Returns the ids moved, so the caller can say how many rather than leaving
    a silent deletion to be discovered later.

    Scoped per site on purpose: three sites each keep their own three. A global
    cap would mean generating for one site evicted another's history.
    """
    rows = (await db.execute(
        active(action_type, site_id)
        # `locked == false` rather than `not locked`: SQLAlchemy needs a SQL
        # expression here, and Python's `not` would evaluate the column object
        # to True and filter nothing at all.
        .where(ReviewItem.locked == false())
        .order_by(ReviewItem.created_at.desc())
    )).scalars().all()

    doomed = rows[max(0, keep):]
    if not doomed:
        return []

    now = datetime.now(UTC)
    for item in doomed:
        item.trashed_at = now
    await db.flush()

    logger.info(
        "Retention: moved %d %s(s) for site %s to trash (keeping newest %d)",
        len(doomed), action_type, site_id, keep,
    )
    return [item.id for item in doomed]


async def purge_expired(
    db: AsyncSession, action_type: str, *, ttl_days: int = TRASH_TTL_DAYS
) -> int:
    """Permanently remove items trashed longer ago than the window allows.

    Locked items are excluded even though a locked item should never be in the
    trash — a belt-and-braces condition, because the one bug this code must
    not have is deleting something a user explicitly protected.
    """
    cutoff = datetime.now(UTC) - timedelta(days=ttl_days)
    result = await db.execute(
        delete(ReviewItem).where(
            ReviewItem.action_type == action_type,
            ReviewItem.trashed_at.isnot(None),
            ReviewItem.trashed_at < cutoff,
            ReviewItem.locked == false(),
        )
    )
    removed = result.rowcount or 0
    if removed:
        logger.info(
            "Retention: purged %d %s(s) trashed before %s", removed, action_type, cutoff
        )
    return removed


async def empty_trash(
    db: AsyncSession, action_type: str, site_id: str | None = None
) -> int:
    """Discard the trash now, rather than waiting out the window."""
    query = delete(ReviewItem).where(
        ReviewItem.action_type == action_type,
        ReviewItem.trashed_at.isnot(None),
        ReviewItem.locked == false(),
    )
    if site_id:
        query = query.where(ReviewItem.site_id == site_id)
    result = await db.execute(query)
    return result.rowcount or 0


__all__ = [
    "KEEP_LATEST",
    "TRASH_TTL_DAYS",
    "active",
    "apply_retention",
    "empty_trash",
    "expires_at",
    "purge_expired",
    "trashed",
]
