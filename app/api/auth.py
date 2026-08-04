"""Google OAuth2 flow — connect GA + GSC."""
import logging
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote, urlencode

import httpx
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import RedirectResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.engine import get_db
from app.database.models import OAuthToken
from app.security.auth import create_state_token, require_user, verify_state_token

logger = logging.getLogger(__name__)

router = APIRouter()

_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"

# Scopes needed: GA4 readonly + Search Console readonly
ANALYTICS_SCOPE = "https://www.googleapis.com/auth/analytics.readonly"
SEARCH_CONSOLE_SCOPE = "https://www.googleapis.com/auth/webmasters.readonly"

_SCOPE_LIST = (ANALYTICS_SCOPE, SEARCH_CONSOLE_SCOPE, "openid", "email")
_SCOPES = " ".join(_SCOPE_LIST)

# Which capability each scope unlocks. Requesting a scope is not the same as
# being granted one: Google's consent screen lets people approve them
# individually, and the sensitive Analytics/Search Console scopes are also
# withheld when the Cloud project's consent screen has not been configured
# for them. Either way the callback returns a perfectly valid token that then
# fails every data call with ACCESS_TOKEN_SCOPE_INSUFFICIENT — so what was
# actually granted has to be checked, not assumed.
CAPABILITY_SCOPES: dict[str, str] = {
    "analytics": ANALYTICS_SCOPE,
    "search_console": SEARCH_CONSOLE_SCOPE,
}


def missing_scopes(granted: str | None) -> list[str]:
    """Required scopes absent from what Google actually granted."""
    have = set((granted or "").split())
    return [scope for scope in CAPABILITY_SCOPES.values() if scope not in have]


def capabilities(granted: str | None) -> dict[str, bool]:
    """Which integrations this token can actually serve."""
    have = set((granted or "").split())
    return {name: scope in have for name, scope in CAPABILITY_SCOPES.items()}


class GoogleStatus(BaseModel):
    connected: bool
    email: str | None = None
    scopes: list[str] = []
    expires_at: datetime | None = None
    # A token can exist and still be useless. These say what it can actually
    # do, so the UI never shows a green tick for a connection that 403s on
    # every request.
    analytics: bool = False
    search_console: bool = False
    missing_scopes: list[str] = []


class GoogleRefreshResult(BaseModel):
    status: str
    message: str


class SiteConfigUpdate(BaseModel):
    ga_property_id: str | None = None
    gsc_site_url: str | None = None


@router.get("/google", dependencies=[Depends(require_user)])
async def google_authorize() -> dict[str, str]:
    """Return the Google OAuth2 authorization URL."""
    if not settings.GA_CLIENT_ID:
        raise HTTPException(status_code=400, detail="GA_CLIENT_ID not configured")

    params = {
        "client_id": settings.GA_CLIENT_ID,
        "redirect_uri": settings.GA_REDIRECT_URI,
        "response_type": "code",
        "scope": _SCOPES,
        "access_type": "offline",
        "prompt": "consent",  # Always get refresh token
        # Keep scopes already approved, so re-connecting to add Analytics
        # cannot silently drop Search Console (or vice versa).
        "include_granted_scopes": "true",
        "state": create_state_token(),  # CSRF: callback must echo this back
    }
    url = f"{_AUTH_URL}?{urlencode(params)}"
    return {"url": url}


@router.get("/google/callback")
async def google_callback(
    code: str,
    state: str = "",
    db: AsyncSession = Depends(get_db),
) -> RedirectResponse:
    """Exchange authorization code for tokens and store them.

    Public by necessity (Google's browser redirect carries no bearer token);
    the signed short-lived `state` proves the flow started from this app.
    """
    if not verify_state_token(state):
        return RedirectResponse(
            url=f"{settings.FRONTEND_URL}/settings?google_error=invalid_state"
        )

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            _TOKEN_URL,
            data={
                "code": code,
                "client_id": settings.GA_CLIENT_ID,
                "client_secret": settings.GA_CLIENT_SECRET,
                "redirect_uri": settings.GA_REDIRECT_URI,
                "grant_type": "authorization_code",
            },
        )
        if resp.status_code != 200:
            return RedirectResponse(
                url=f"{settings.FRONTEND_URL}/settings?google_error=token_exchange_failed"
            )
        token_data = resp.json()

    access_token: str = token_data["access_token"]
    refresh_token: str = token_data.get("refresh_token", "")
    expires_in: int = token_data.get("expires_in", 3600)
    scope: str = token_data.get("scope", "")
    expiry = datetime.now(UTC) + timedelta(seconds=expires_in - 60)

    if not refresh_token:
        return RedirectResponse(
            url=f"{settings.FRONTEND_URL}/settings?google_error=no_refresh_token"
        )

    # Upsert token record
    existing = await db.execute(select(OAuthToken).where(OAuthToken.provider == "google"))
    token_record = existing.scalar_one_or_none()

    if token_record:
        token_record.access_token = access_token
        token_record.refresh_token = refresh_token
        token_record.token_expiry = expiry
        token_record.scope = scope
    else:
        token_record = OAuthToken(
            provider="google",
            access_token=access_token,
            refresh_token=refresh_token,
            token_expiry=expiry,
            scope=scope,
        )
        db.add(token_record)

    # Google returns a valid token even when the sensitive scopes were not
    # approved. Saying "connected" at that point is how every Analytics call
    # ends up returning ACCESS_TOKEN_SCOPE_INSUFFICIENT into a log file while
    # the UI shows a green tick — so the outcome is reported honestly instead.
    absent = missing_scopes(scope)
    if absent:
        logger.warning(
            "Google connected WITHOUT required scope(s): %s — granted: %s. "
            "Analytics and Search Console calls will fail with 403 until "
            "these are approved.",
            ", ".join(absent), scope or "(none)",
        )
        return RedirectResponse(
            url=f"{settings.FRONTEND_URL}/settings?google_partial=1"
            f"&missing={quote(' '.join(absent))}"
        )

    return RedirectResponse(url=f"{settings.FRONTEND_URL}/settings?google_connected=1")


@router.get("/google/status", response_model=GoogleStatus, dependencies=[Depends(require_user)])
async def google_status(db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    """Check if Google is connected."""
    result = await db.execute(select(OAuthToken).where(OAuthToken.provider == "google"))
    token = result.scalar_one_or_none()
    if not token:
        return {"connected": False}
    return {
        "connected": True,
        "scopes": token.scope.split() if token.scope else [],
        "expires_at": token.token_expiry,
        **capabilities(token.scope),
        "missing_scopes": missing_scopes(token.scope),
    }


@router.delete("/google", dependencies=[Depends(require_user)])
async def google_disconnect(db: AsyncSession = Depends(get_db)) -> dict[str, str]:
    """Remove stored Google tokens."""
    result = await db.execute(select(OAuthToken).where(OAuthToken.provider == "google"))
    token = result.scalar_one_or_none()
    if token:
        await db.delete(token)
    return {"status": "disconnected"}


@router.post("/google/refresh", response_model=GoogleRefreshResult, dependencies=[Depends(require_user)])
async def google_refresh(db: AsyncSession = Depends(get_db)) -> dict[str, str]:
    """Force-renew the access token right now, instead of waiting for the
    lazy refresh in get_google_token(). Lets Settings verify a connection is
    still healthy on demand, and surfaces a clear "reconnect" signal if the
    refresh token itself has been revoked — rather than staying silently
    stuck showing "Connected" while every Google API call fails in the
    background.
    """
    result = await db.execute(select(OAuthToken).where(OAuthToken.provider == "google"))
    token = result.scalar_one_or_none()
    if not token:
        raise HTTPException(status_code=404, detail="Google is not connected")

    async with httpx.AsyncClient() as client:
        resp = await client.post(
            _TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": settings.GA_CLIENT_ID,
                "client_secret": settings.GA_CLIENT_SECRET,
                "refresh_token": token.refresh_token,
            },
        )

    if resp.status_code != 200:
        # The refresh token is dead (revoked/expired) — clear the row so the
        # UI falls back to "Connect Google" instead of a stale, false "Connected".
        await db.delete(token)
        raise HTTPException(
            status_code=400,
            detail="Google connection has expired or was revoked — please reconnect.",
        )

    data = resp.json()
    token.access_token = data["access_token"]
    token.token_expiry = datetime.now(UTC) + timedelta(seconds=data.get("expires_in", 3600) - 60)
    await db.flush()
    return {"status": "refreshed", "message": "Google connection refreshed."}


async def get_google_token(db: AsyncSession) -> OAuthToken | None:
    """Helper used by connectors to get a valid token, refreshing if needed."""
    result = await db.execute(select(OAuthToken).where(OAuthToken.provider == "google"))
    token = result.scalar_one_or_none()
    if not token:
        return None

    # Refresh if expired or expiring in < 5 minutes
    now = datetime.now(UTC)
    if token.token_expiry and token.token_expiry <= now + timedelta(minutes=5):
        async with httpx.AsyncClient() as client:
            resp = await client.post(
                _TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "client_id": settings.GA_CLIENT_ID,
                    "client_secret": settings.GA_CLIENT_SECRET,
                    "refresh_token": token.refresh_token,
                },
            )
            if resp.status_code == 200:
                data = resp.json()
                token.access_token = data["access_token"]
                token.token_expiry = now + timedelta(seconds=data.get("expires_in", 3600) - 60)
                await db.flush()

    return token
