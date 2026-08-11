import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import router
from app.config import settings
from app.database.engine import init_db
from app.security.startup_checks import verify_settings
from app.services.scheduler import scheduler, setup_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Before anything touches the database. A deployment that will not start
    # is a problem you have on day one; one that starts with the published
    # dev SECRET_KEY is a problem you have on the day someone else finds it.
    verify_settings(settings)

    if settings.AUTO_CREATE_SCHEMA:
        await init_db()

    from app.database.engine import AsyncSessionLocal
    from app.security.auth import ensure_initial_admin
    async with AsyncSessionLocal() as db:
        await ensure_initial_admin(db)

    setup_scheduler()
    yield
    scheduler.shutdown(wait=False)


app = FastAPI(
    title="WP Command Center API",
    description="AI-powered WordPress multi-site operations platform",
    version="0.1.0",
    lifespan=lifespan,
    # The interactive docs enumerate every route, parameter and schema. That
    # is a gift to a developer and a map for anyone probing the deployment,
    # so production serves neither them nor the spec they are built from.
    docs_url=None if settings.is_production else "/docs",
    redoc_url=None if settings.is_production else "/redoc",
    openapi_url=None if settings.is_production else "/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    # Authentication is a bearer token from localStorage, never a cookie, so
    # credentialed cross-origin requests are not something this API needs to
    # permit — and allowing them widens what a hostile origin could attempt.
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=["Authorization", "Content-Type"],
)

app.include_router(router, prefix="/api")


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok", "version": "0.1.0"}


@app.get("/ready")
async def ready() -> dict[str, str]:
    """Readiness probe — verifies the database is reachable."""
    from fastapi import HTTPException
    from sqlalchemy import text

    from app.database.engine import engine

    try:
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
    except Exception as exc:
        raise HTTPException(status_code=503, detail="database unavailable") from exc
    return {"status": "ready"}
