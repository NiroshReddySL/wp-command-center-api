"""User authentication endpoints — login, current user, and user management."""
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.engine import get_db
from app.database.models import User
from app.security.auth import (
    CurrentUser,
    create_access_token,
    hash_password,
    require_admin,
    require_user,
    verify_password,
)
from app.security.rate_limit import login_limiter

router = APIRouter()


class LoginRequest(BaseModel):
    # Plain str, not EmailStr: this is a lookup key and must accept whatever
    # address an account was created with (incl. the admin@wpcc.local bootstrap).
    email: str
    password: str


class LoginResponse(BaseModel):
    access_token: str
    token_type: str = "bearer"
    email: str
    role: str


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    role: str = Field(default="member", pattern="^(admin|member)$")


class UserResponse(BaseModel):
    id: str
    email: str
    role: str
    created_at: datetime


class PasswordChange(BaseModel):
    current_password: str
    new_password: str = Field(min_length=8)


@router.post("/login", response_model=LoginResponse, dependencies=[Depends(login_limiter)])
async def login(body: LoginRequest, db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    result = await db.execute(select(User).where(User.email == body.email.lower()))
    user = result.scalar_one_or_none()

    # Same error for unknown email and wrong password — no account enumeration
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Invalid email or password")

    return {
        "access_token": create_access_token(user.id, user.role),
        "token_type": "bearer",
        "email": user.email,
        "role": user.role,
    }


@router.get("/me", response_model=UserResponse)
async def me(
    current: CurrentUser = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> User:
    result = await db.execute(select(User).where(User.id == current.id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    return user


@router.put("/me/password", status_code=204)
async def change_password(
    body: PasswordChange,
    current: CurrentUser = Depends(require_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    result = await db.execute(select(User).where(User.id == current.id))
    user = result.scalar_one_or_none()
    if not user or not verify_password(body.current_password, user.password_hash):
        raise HTTPException(status_code=401, detail="Current password is incorrect")
    user.password_hash = hash_password(body.new_password)


@router.get("/users", response_model=list[UserResponse], dependencies=[Depends(require_admin)])
async def list_users(db: AsyncSession = Depends(get_db)) -> list[User]:
    result = await db.execute(select(User).order_by(User.created_at.asc()))
    return list(result.scalars().all())


@router.post("/users", response_model=UserResponse, status_code=201, dependencies=[Depends(require_admin)])
async def create_user(body: UserCreate, db: AsyncSession = Depends(get_db)) -> User:
    existing = await db.execute(select(User).where(User.email == body.email.lower()))
    if existing.scalar_one_or_none():
        raise HTTPException(status_code=409, detail="A user with this email already exists")

    user = User(
        email=body.email.lower(),
        password_hash=hash_password(body.password),
        role=body.role,
    )
    db.add(user)
    await db.flush()
    return user


@router.delete("/users/{user_id}", status_code=204)
async def delete_user(
    user_id: str,
    current: CurrentUser = Depends(require_admin),
    db: AsyncSession = Depends(get_db),
) -> None:
    if user_id == current.id:
        raise HTTPException(status_code=422, detail="You cannot delete your own account")
    result = await db.execute(select(User).where(User.id == user_id))
    user = result.scalar_one_or_none()
    if not user:
        raise HTTPException(status_code=404, detail="User not found")
    await db.delete(user)
