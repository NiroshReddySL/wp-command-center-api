from typing import Any

from fastapi import APIRouter, Depends
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.engine import get_db
from app.database.models import Alert, ContentPost, Site

router = APIRouter()


@router.get("")
async def search(q: str = "", db: AsyncSession = Depends(get_db)) -> dict[str, Any]:
    q = q.strip()
    if not q or len(q) < 2:
        return {"sites": [], "alerts": [], "posts": []}

    term = f"%{q.lower()}%"

    # Sites
    sites_r = await db.execute(
        select(Site)
        .where(or_(Site.name.ilike(term), Site.url.ilike(term)))
        .limit(5)
    )
    sites = [
        {"id": s.id, "name": s.name, "url": s.url, "health_score": s.health_score, "status": s.status}
        for s in sites_r.scalars().all()
    ]

    # Alerts (open only)
    alerts_r = await db.execute(
        select(Alert, Site.name.label("site_name"))
        .join(Site, Alert.site_id == Site.id)
        .where(
            Alert.status.in_(["open", "acknowledged"]),
            or_(Alert.title.ilike(term), Alert.description.ilike(term)),
        )
        .order_by(Alert.created_at.desc())
        .limit(5)
    )
    alerts = [
        {
            "id": a.id,
            "title": a.title,
            "severity": a.severity,
            "agent": a.agent,
            "site_name": site_name,
            "site_id": a.site_id,
            "type": a.type,
        }
        for a, site_name in alerts_r.all()
    ]

    # Content posts
    posts_r = await db.execute(
        select(ContentPost, Site.name.label("site_name"))
        .join(Site, ContentPost.site_id == Site.id)
        .where(or_(ContentPost.title.ilike(term), ContentPost.url.ilike(term)))
        .order_by(ContentPost.health_score.asc())
        .limit(5)
    )
    posts = [
        {
            "id": p.id,
            "title": p.title,
            "url": p.url,
            "health_score": p.health_score,
            "site_name": site_name,
            "site_id": p.site_id,
        }
        for p, site_name in posts_r.all()
    ]

    return {"sites": sites, "alerts": alerts, "posts": posts}
