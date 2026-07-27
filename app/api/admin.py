"""Admin utilities — full data reset for dev/test cleanup."""
from fastapi import APIRouter, Depends
from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.engine import get_db
from app.database.models import (
    Alert, ContentPost, OAuthToken, PerformanceSnapshot,
    PluginAudit, ReviewItem, Site, SiteConfig,
    TrafficPrediction, TrafficSnapshot, Variant,
)

router = APIRouter()


@router.post("/reset")
async def reset_all_data(db: AsyncSession = Depends(get_db)) -> dict[str, str]:
    """Delete all application data. Sites, agents, snapshots, predictions — everything."""
    # Order matters: delete dependents before parents
    for model in (
        TrafficPrediction,
        TrafficSnapshot,
        PerformanceSnapshot,
        PluginAudit,
        Variant,
        ReviewItem,
        Alert,
        ContentPost,
        SiteConfig,
        OAuthToken,
        Site,
    ):
        await db.execute(delete(model))

    await db.commit()
    return {"status": "reset", "message": "All data deleted successfully."}
