"""Google OAuth2 flow — connect GA + GSC."""
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

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

router = APIRouter()

_AUTH_URL = "https://accounts.google.com/o/oauth2/v2/auth"
_TOKEN_URL = "https://oauth2.googleapis.com/token"

# Scopes needed: GA4 readonly + Search Console readonly
_SCOPES = " ".join([
    "https://www.googleapis.com/auth/analytics.readonly",
    "https://www.googleapis.com/auth/webmasters.readonly",
    "openid",
    "email",
])


class GoogleStatus(BaseModel):
    connected: bool
    email: str | None = None
    scopes: list[str] = []


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
    expiry = datetime.now(timezone.utc) + timedelta(seconds=expires_in - 60)

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
    }


@router.delete("/google", dependencies=[Depends(require_user)])
async def google_disconnect(db: AsyncSession = Depends(get_db)) -> dict[str, str]:
    """Remove stored Google tokens."""
    result = await db.execute(select(OAuthToken).where(OAuthToken.provider == "google"))
    token = result.scalar_one_or_none()
    if token:
        await db.delete(token)
    return {"status": "disconnected"}


async def get_google_token(db: AsyncSession) -> OAuthToken | None:
    """Helper used by connectors to get a valid token, refreshing if needed."""
    result = await db.execute(select(OAuthToken).where(OAuthToken.provider == "google"))
    token = result.scalar_one_or_none()
    if not token:
        return None

    # Refresh if expired or expiring in < 5 minutes
    now = datetime.now(timezone.utc)
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
