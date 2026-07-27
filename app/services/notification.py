"""Alert notification dispatcher — Microsoft Teams.

Sends Adaptive Cards in the `{"type": "message", "attachments": [...]}`
envelope, which is what Power Automate Workflows webhooks ("Post to a channel
when a webhook request is received") expect. Legacy Office 365 connector URLs
accept the same payload. The webhook URL is configured in Settings →
Notification Preferences (stored encrypted), with the TEAMS_WEBHOOK_URL env
var as a fallback.
"""
import logging
from typing import Any

import httpx

from app.config import settings
from app.database.models import Alert
from app.services.app_settings import get_notification_prefs

logger = logging.getLogger(__name__)

# Adaptive Card TextBlock colors per alert severity
SEVERITY_COLOR = {"critical": "Attention", "warning": "Warning", "info": "Accent"}
SEVERITY_EMOJI = {"critical": "🔴", "warning": "🟡", "info": "🔵"}


def _card_envelope(body: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "type": "message",
        "attachments": [
            {
                "contentType": "application/vnd.microsoft.card.adaptive",
                "content": {
                    "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
                    "type": "AdaptiveCard",
                    "version": "1.4",
                    "msteams": {"width": "Full"},
                    "body": body,
                },
            }
        ],
    }


def build_alert_card(alert: Alert, site_name: str) -> dict[str, Any]:
    emoji = SEVERITY_EMOJI.get(alert.severity, "⚪")
    return _card_envelope([
        {
            "type": "TextBlock",
            "text": f"{emoji} [{alert.severity.upper()}] {alert.title}",
            "weight": "Bolder",
            "size": "Medium",
            "color": SEVERITY_COLOR.get(alert.severity, "Default"),
            "wrap": True,
        },
        {
            "type": "FactSet",
            "facts": [
                {"title": "Site", "value": site_name},
                {"title": "Agent", "value": alert.agent},
            ],
        },
        {"type": "TextBlock", "text": alert.description, "wrap": True, "spacing": "Small"},
    ])


def build_digest_card(site_rows: list[tuple[str, int]]) -> dict[str, Any]:
    """Weekly digest — one FactSet row per site: name → health score."""
    return _card_envelope([
        {
            "type": "TextBlock",
            "text": "📋 Weekly reports are ready",
            "weight": "Bolder",
            "size": "Medium",
            "wrap": True,
        },
        {
            "type": "TextBlock",
            "text": f"WP Command Center generated reports for {len(site_rows)} site(s). "
                    "Open the Review Queue to read them.",
            "wrap": True,
            "spacing": "Small",
        },
        {
            "type": "FactSet",
            "facts": [
                {"title": name, "value": f"Health {score}/100"} for name, score in site_rows
            ],
        },
    ])


def build_test_card() -> dict[str, Any]:
    return _card_envelope([
        {
            "type": "TextBlock",
            "text": "✅ WP Command Center connected",
            "weight": "Bolder",
            "size": "Medium",
        },
        {
            "type": "TextBlock",
            "text": "Test message — critical alerts and weekly digests will arrive in this channel.",
            "wrap": True,
        },
    ])


async def send_teams_message(webhook_url: str, payload: dict[str, Any]) -> None:
    """POST a card to the Teams webhook. Raises on HTTP failure."""
    async with httpx.AsyncClient() as client:
        response = await client.post(webhook_url, json=payload, timeout=10.0)
        response.raise_for_status()


async def dispatch_alert_notification(db: Any, alert: Alert, site_name: str) -> None:
    """Send critical alerts to Teams when a webhook is configured and enabled."""
    prefs = await get_notification_prefs(db)
    webhook_url = prefs["teams_webhook_url"] or settings.TEAMS_WEBHOOK_URL
    if not webhook_url or not prefs["notify_critical"] or alert.severity != "critical":
        return
    try:
        await send_teams_message(webhook_url, build_alert_card(alert, site_name))
    except Exception as exc:
        logger.error("Failed to send Teams notification: %s", exc)
