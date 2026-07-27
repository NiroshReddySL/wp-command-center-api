"""Application settings stored in the DB — agent toggles and notification prefs.

The AGENT_DEFINITIONS list is the single source of truth for which agents can
be toggled: the API serves it to the Settings UI and the scheduler reads the
same keys before each run, so the panel can never drift from real behavior.
Toggles gate SCHEDULED runs only — a manual "Run agents" always runs everything.
"""
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import AppSetting
from app.security.crypto import decrypt_value, encrypt_value

AGENT_TOGGLES_KEY = "agent_toggles"
NOTIFICATION_PREFS_KEY = "notification_prefs"

AGENT_DEFINITIONS: list[dict[str, str]] = [
    {"key": "watchdog.links", "label": "Broken Link Checker",
     "description": "Crawl posts and detect broken links every 6 hours."},
    {"key": "watchdog.plugins", "label": "Plugin Audit",
     "description": "Check for outdated or vulnerable plugins every 6 hours."},
    {"key": "watchdog.performance", "label": "Performance Monitor",
     "description": "Measure page response times every 2 hours and alert on regressions."},
    {"key": "optimizer.seo", "label": "SEO Analyzer",
     "description": "Analyze SEO issues, ranking opportunities, and internal links daily at 03:00 UTC."},
    {"key": "optimizer.content", "label": "Content Scorer",
     "description": "Score all posts for freshness, headings, links, and images daily at 03:00 UTC."},
    {"key": "traffic.sync", "label": "Traffic Sync",
     "description": "Pull GA4 / Search Console snapshots nightly and refresh traffic predictions."},
    {"key": "autopilot.reports", "label": "Automated Reports",
     "description": "Generate weekly performance reports every Friday at 06:00 UTC."},
    {"key": "flows.classify", "label": "Flow Categories",
     "description": "Classify yesterday's activity into your defined flow categories nightly at 04:00 UTC."},
]

KNOWN_AGENT_KEYS = {d["key"] for d in AGENT_DEFINITIONS}

NOTIFICATION_DEFAULTS: dict[str, Any] = {
    "teams_webhook_url": "",
    "notify_critical": True,
    "weekly_digest": True,
}


def merge_agent_toggles(stored: dict[str, Any] | None) -> dict[str, bool]:
    """Stored state over defaults — unknown stored keys are dropped, missing default to on."""
    stored = stored or {}
    return {key: bool(stored.get(key, True)) for key in KNOWN_AGENT_KEYS}


async def _get_value(db: AsyncSession, key: str) -> dict[str, Any]:
    row = await db.get(AppSetting, key)
    return dict(row.value) if row and isinstance(row.value, dict) else {}


async def _set_value(db: AsyncSession, key: str, value: dict[str, Any]) -> None:
    row = await db.get(AppSetting, key)
    if row is None:
        db.add(AppSetting(key=key, value=value))
    else:
        row.value = value
    await db.flush()


async def get_agent_toggles(db: AsyncSession) -> dict[str, bool]:
    return merge_agent_toggles(await _get_value(db, AGENT_TOGGLES_KEY))


async def set_agent_toggles(db: AsyncSession, toggles: dict[str, bool]) -> dict[str, bool]:
    current = merge_agent_toggles(await _get_value(db, AGENT_TOGGLES_KEY))
    current.update({k: bool(v) for k, v in toggles.items() if k in KNOWN_AGENT_KEYS})
    await _set_value(db, AGENT_TOGGLES_KEY, current)
    return current


async def get_notification_prefs(db: AsyncSession) -> dict[str, Any]:
    stored = await _get_value(db, NOTIFICATION_PREFS_KEY)
    prefs = {**NOTIFICATION_DEFAULTS, **{k: stored[k] for k in NOTIFICATION_DEFAULTS if k in stored}}
    if prefs["teams_webhook_url"]:
        prefs["teams_webhook_url"] = decrypt_value(prefs["teams_webhook_url"])
    return prefs


async def set_notification_prefs(db: AsyncSession, prefs: dict[str, Any]) -> None:
    to_store = {**NOTIFICATION_DEFAULTS, **{k: prefs[k] for k in NOTIFICATION_DEFAULTS if k in prefs}}
    if to_store["teams_webhook_url"]:
        to_store["teams_webhook_url"] = encrypt_value(str(to_store["teams_webhook_url"]))
    to_store["notify_critical"] = bool(to_store["notify_critical"])
    to_store["weekly_digest"] = bool(to_store["weekly_digest"])
    await _set_value(db, NOTIFICATION_PREFS_KEY, to_store)
