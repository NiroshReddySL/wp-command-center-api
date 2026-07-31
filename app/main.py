import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.router import router
from app.config import settings
from app.database.engine import init_db
from app.services.scheduler import scheduler, setup_scheduler

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(name)s — %(message)s",
)


@asynccontextmanager
async def lifespan(app: FastAPI):
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
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins_list,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
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
