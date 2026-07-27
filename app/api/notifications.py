from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.engine import get_db
from app.database.models import Alert, Site

router = APIRouter()


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


@router.get("", response_model=list[NotificationResponse])
async def list_notifications(
    db: AsyncSession = Depends(get_db),
) -> list[dict[str, Any]]:
    """Return the 20 most recent open critical/warning alerts as notifications."""
    result = await db.execute(
        select(Alert, Site.name.label("site_name"))
        .join(Site, Alert.site_id == Site.id)
        .where(
            Alert.status.in_(["open", "acknowledged"]),
            Alert.severity.in_(["critical", "warning"]),
        )
        .order_by(Alert.created_at.desc())
        .limit(20)
    )
    rows = result.all()
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
        for alert, site_name in rows
    ]


@router.get("/count")
async def notification_count(db: AsyncSession = Depends(get_db)) -> dict[str, int]:
    """Fast count of unread (open) critical + warning alerts."""
    from sqlalchemy import func
    result = await db.execute(
        select(func.count(Alert.id)).where(
            Alert.status == "open",
            Alert.severity.in_(["critical", "warning"]),
        )
    )
    count = result.scalar_one() or 0
    return {"count": count}
