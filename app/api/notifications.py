"""The notification bell.

Scoped to the selected site when one is selected. Without that the bell
contradicted the rest of the app: every other view honours the site picker, so
a badge counting three sites' alerts while the page beneath showed one site's
made the number unattributable — you could not tell which site it was talking
about, and clearing the page's findings did not move it.

The list and the count are built from one filter for the same reason: a badge
saying 7 above a list of 3 is a bug report waiting to happen, and the two
queries drifting apart is how that happens.
"""
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel
from sqlalchemy import Select, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.engine import get_db
from app.database.models import Alert, Site

router = APIRouter()

# What counts as worth interrupting someone about. "info" alerts are findings
# to read at leisure, not notifications.
NOTIFY_SEVERITIES = ("critical", "warning")
RECENT_LIMIT = 20


class NotificationResponse(BaseModel):
    id: str
    site_id: str
    site_name: str
    agent: str
    severity: str
    type: str
    title: str
    description: str
    status: str
    created_at: datetime


def _scoped(query: Select, site_id: str | None, *, open_only: bool) -> Select:
    """The one definition of "a notification", used by both endpoints."""
    query = query.where(Alert.severity.in_(NOTIFY_SEVERITIES))
    query = query.where(
        Alert.status == "open" if open_only
        else Alert.status.in_(["open", "acknowledged"])
    )
    if site_id:
        query = query.where(Alert.site_id == site_id)
    return query


@router.get("", response_model=list[NotificationResponse])
async def list_notifications(
    site_id: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    """The most recent open critical/warning alerts, for one site or all."""
    result = await db.execute(
        _scoped(
            select(Alert, Site.name.label("site_name")).join(Site, Alert.site_id == Site.id),
            site_id, open_only=False,
        )
        .order_by(Alert.created_at.desc())
        .limit(RECENT_LIMIT)
    )
    return [
        {
            "id": alert.id,
            "site_id": alert.site_id,
            "site_name": site_name,
            "agent": alert.agent,
            "severity": alert.severity,
            "type": alert.type,
            "title": alert.title,
            "description": alert.description,
            "status": alert.status,
            "created_at": alert.created_at,
        }
        for alert, site_name in result.all()
    ]


@router.get("/count")
async def notification_count(
    site_id: str | None = Query(default=None),
    db: AsyncSession = Depends(get_db),
) -> dict[str, int]:
    """Unread (open) critical + warning alerts, for the same scope as the list."""
    result = await db.execute(
        _scoped(select(func.count(Alert.id)), site_id, open_only=True)
    )
    return {"count": result.scalar_one() or 0}
