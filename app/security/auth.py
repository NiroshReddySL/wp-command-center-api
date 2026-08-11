"""Authentication core — password hashing, JWTs, and route dependencies.

Every data route is protected by `require_user`; destructive/administrative
routes use `require_admin`. Tokens are accepted from the `Authorization:
Bearer` header, or — for EventSource/SSE endpoints, which cannot set headers
— from a `token` query parameter.
"""
import logging
import secrets
from datetime import UTC, datetime, timedelta

import bcrypt
from fastapi import Depends, HTTPException, Query, Request
from jose import JWTError, jwt
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database.engine import get_db
from app.database.models import User

logger = logging.getLogger(__name__)

_ALGORITHM = "HS256"


# ── Passwords ─────────────────────────────────────────────────────────────────

def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()


def verify_password(password: str, password_hash: str) -> bool:
    try:
        return bcrypt.checkpw(password.encode(), password_hash.encode())
    except ValueError:
        return False


# ── Tokens ────────────────────────────────────────────────────────────────────

def create_access_token(user_id: str, role: str) -> str:
    expires = datetime.now(UTC) + timedelta(hours=settings.JWT_EXPIRY_HOURS)
    payload = {"sub": user_id, "role": role, "type": "access", "exp": expires}
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=_ALGORITHM)


def _decode_token(token: str) -> dict:
    return jwt.decode(token, settings.SECRET_KEY, algorithms=[_ALGORITHM])


# ── OAuth CSRF state ──────────────────────────────────────────────────────────

def create_state_token() -> str:
    """Short-lived signed nonce binding an OAuth callback to this app."""
    expires = datetime.now(UTC) + timedelta(minutes=10)
    payload = {"nonce": secrets.token_urlsafe(16), "type": "oauth_state", "exp": expires}
    return jwt.encode(payload, settings.SECRET_KEY, algorithm=_ALGORITHM)


def verify_state_token(state: str) -> bool:
    try:
        payload = _decode_token(state)
        return payload.get("type") == "oauth_state"
    except JWTError:
        return False


# ── Route dependencies ────────────────────────────────────────────────────────

class CurrentUser(BaseModel):
    id: str
    email: str
    role: str  # "admin" | "member"


async def require_user(
    request: Request,
    db: AsyncSession = Depends(get_db),
    token_qp: str | None = Query(default=None, alias="token"),
) -> CurrentUser:
    auth_header = request.headers.get("Authorization", "")
    token = auth_header.removeprefix("Bearer ").strip() if auth_header.startswith("Bearer ") else token_qp

    if not token:
        raise HTTPException(status_code=401, detail="Not authenticated")

    try:
        payload = _decode_token(token)
    except JWTError:
        raise HTTPException(status_code=401, detail="Invalid or expired token") from None

    if payload.get("type") != "access" or not payload.get("sub"):
        raise HTTPException(status_code=401, detail="Invalid or expired token")

    result = await db.execute(select(User).where(User.id == payload["sub"]))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=401, detail="User no longer exists")

    return CurrentUser(id=user.id, email=user.email, role=user.role)


async def require_admin(user: CurrentUser = Depends(require_user)) -> CurrentUser:
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="Admin access required")
    return user


# ── Bootstrap ─────────────────────────────────────────────────────────────────

async def ensure_initial_admin(db: AsyncSession) -> None:
    """Create the first admin account when the users table is empty."""
    count = (await db.execute(select(func.count(User.id)))).scalar_one()
    if count > 0:
        return

    if settings.is_production and not settings.ADMIN_PASSWORD:
        # Generating one would write a live admin credential into the startup
        # log, and production logs are shipped, indexed and retained. Better
        # to have no account and say so than an account whose password is
        # sitting in a log aggregator.
        logger.error(
            "No users exist and ADMIN_PASSWORD is unset. Refusing to generate one in "
            "production, because it would be logged in plain text. Set ADMIN_PASSWORD "
            "and restart to create the first account."
        )
        return

    email = settings.ADMIN_EMAIL or "admin@wpcc.local"
    password = settings.ADMIN_PASSWORD or secrets.token_urlsafe(12)

    db.add(User(email=email, password_hash=hash_password(password), role="admin"))
    await db.commit()

    if settings.ADMIN_PASSWORD:
        logger.info("Initial admin account created: %s", email)
    else:
        # Development only — see the production branch above.
        logger.warning(
            "Initial admin account created: %s / %s — log in and change this password",
            email, password,
        )
