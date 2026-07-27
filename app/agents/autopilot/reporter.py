"""Reporter — generates weekly site performance reports."""
from datetime import datetime, timedelta, timezone

from sqlalchemy import select

from app.agents.base import BaseAgent
from app.ai.engine import ai_engine
from app.database.models import Alert, ContentPost, ReviewItem, Site


class Reporter(BaseAgent):
    async def run(self, site_id: str) -> list[Alert]:
        result = await self.db.execute(select(Site).where(Site.id == site_id))
        site = result.scalar_one_or_none()
        if not site:
            return []

        week_ago = datetime.now(timezone.utc) - timedelta(days=7)

        alerts_result = await self.db.execute(
            select(Alert)
            .where(Alert.site_id == site_id, Alert.created_at >= week_ago)
            .order_by(Alert.created_at.desc())
        )
        week_alerts = alerts_result.scalars().all()

        posts_result = await self.db.execute(
            select(ContentPost)
            .where(ContentPost.site_id == site_id)
            .order_by(ContentPost.health_score.desc())
            .limit(5)
        )
        top_posts = posts_result.scalars().all()

        alert_lines = "\n".join(
            f"- [{a.severity.upper()}] {a.title}" for a in week_alerts[:10]
        ) or "No new alerts this week."

        posts_lines = "\n".join(
            f"- {p.title} (score: {p.health_score}/100)" for p in top_posts
        ) or "No content data available."

        prompt = (
            f'Generate a concise weekly WordPress site report for "{site.name}".\n\n'
            f"Issues this week:\n{alert_lines}\n\n"
            f"Top content:\n{posts_lines}\n\n"
            f"Site health score: {site.health_score}/100\n\n"
            "Write 150–200 words covering:\n"
            "1. Most important issues to address\n"
            "2. Content highlights\n"
            "3. Two or three concrete next steps\n\n"
            "Use a direct, professional tone. Return plain text only."
        )

        try:
            narrative = await ai_engine.analyze(prompt)
        except Exception:
            narrative = (
                f"Weekly report for {site.name}: {len(week_alerts)} issues detected. "
                f"Site health: {site.health_score}/100."
            )

        review = ReviewItem(
            agent="autopilot",
            action_type="weekly_report",
            payload={
                "title": f"Weekly Report — {site.name} — {datetime.now(timezone.utc).strftime('%b %d, %Y')}",
                "type": "weekly",
                "narrative": narrative,
                "stats": {
                    "alerts_count": len(week_alerts),
                    "health_score": site.health_score,
                    "top_posts": [{"title": p.title, "score": p.health_score} for p in top_posts],
                },
            },
            status="pending",
            site_id=site_id,
        )
        self.db.add(review)
        await self.db.flush()
        return []
