"""Settings endpoints — agent toggles and notification preferences."""
import logging

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.engine import get_db
from app.security.rate_limit import job_limiter
from app.security.url_guard import ensure_public_url
from app.services.app_settings import (
    AGENT_DEFINITIONS,
    KNOWN_AGENT_KEYS,
    get_agent_toggles,
    get_notification_prefs,
    set_agent_toggles,
    set_notification_prefs,
)
from app.services.notification import build_test_card, send_teams_message

logger = logging.getLogger(__name__)
router = APIRouter()


# ── Agent toggles ─────────────────────────────────────────────────────────────

class AgentToggleOut(BaseModel):
    key: str
    label: str
    description: str
    enabled: bool


class AgentTogglesUpdate(BaseModel):
    toggles: dict[str, bool]


def _to_agent_list(state: dict[str, bool]) -> list[AgentToggleOut]:
    return [AgentToggleOut(**d, enabled=state[d["key"]]) for d in AGENT_DEFINITIONS]


@router.get("/agents", response_model=list[AgentToggleOut])
async def list_agent_toggles(db: AsyncSession = Depends(get_db)) -> list[AgentToggleOut]:
    return _to_agent_list(await get_agent_toggles(db))


@router.put("/agents", response_model=list[AgentToggleOut])
async def update_agent_toggles(
    payload: AgentTogglesUpdate, db: AsyncSession = Depends(get_db)
) -> list[AgentToggleOut]:
    unknown = set(payload.toggles) - KNOWN_AGENT_KEYS
    if unknown:
        raise HTTPException(status_code=422, detail=f"Unknown agent keys: {sorted(unknown)}")
    state = await set_agent_toggles(db, payload.toggles)
    await db.commit()
    return _to_agent_list(state)


# ── Notification preferences ──────────────────────────────────────────────────

class NotificationPrefs(BaseModel):
    teams_webhook_url: str = Field(default="", max_length=2048)
    notify_critical: bool = True
    weekly_digest: bool = True


@router.get("/notifications", response_model=NotificationPrefs)
async def read_notification_prefs(db: AsyncSession = Depends(get_db)) -> NotificationPrefs:
    return NotificationPrefs(**await get_notification_prefs(db))


@router.put("/notifications", response_model=NotificationPrefs)
async def update_notification_prefs(
    payload: NotificationPrefs, db: AsyncSession = Depends(get_db)
) -> NotificationPrefs:
    url = payload.teams_webhook_url.strip()
    if url:
        if not url.startswith("https://"):
            raise HTTPException(status_code=422, detail="Webhook URL must use https://")
        await ensure_public_url(url)  # SSRF guard — user-supplied outbound URL
    await set_notification_prefs(db, {**payload.model_dump(), "teams_webhook_url": url})
    await db.commit()
    return NotificationPrefs(**await get_notification_prefs(db))


@router.post("/notifications/test", dependencies=[Depends(job_limiter)])
async def test_teams_webhook(db: AsyncSession = Depends(get_db)) -> dict[str, str]:
    """Send a test card to the saved (or env-fallback) Teams webhook."""
    prefs = await get_notification_prefs(db)
    webhook_url = prefs["teams_webhook_url"] or settings.TEAMS_WEBHOOK_URL
    if not webhook_url:
        raise HTTPException(status_code=400, detail="No Teams webhook URL configured — save one first.")
    try:
        await send_teams_message(webhook_url, build_test_card())
    except Exception as exc:
        logger.warning("Teams test message failed: %s", exc)
        raise HTTPException(status_code=502, detail=f"Teams rejected the message: {exc}") from exc
    return {"status": "sent"}
