"""Content Repurposer — generates LinkedIn, Twitter, and Email variants for top posts."""
import asyncio

from sqlalchemy import select

from app.agents.base import BaseAgent
from app.ai.engine import ai_engine
from app.database.models import Alert, ContentPost, ReviewItem, Site, Variant

_PROMPTS: dict[str, str] = {
    "linkedin": (
        "Transform this blog post into a compelling LinkedIn post.\n"
        "Post title: {title}\nPost URL: {url}\n\n"
        "Write 150–200 words that:\n"
        "- Open with a strong hook (not 'Excited to share')\n"
        "- Share the key insight or practical value\n"
        "- End with a call-to-action mentioning the link\n"
        "- Use professional tone, max 3 relevant hashtags\n\n"
        "Return only the post content, no explanations."
    ),
    "twitter": (
        "Transform this blog post into a tweet.\n"
        "Post title: {title}\nPost URL: {url}\n\n"
        "Write a single tweet under 280 characters that:\n"
        "- Leads with the key insight\n"
        "- Creates curiosity\n"
        "- Includes the URL at the end\n\n"
        "Return only the tweet, no explanations."
    ),
    "email": (
        "Transform this blog post into an email newsletter excerpt.\n"
        "Post title: {title}\nPost URL: {url}\n\n"
        "Write 100–150 words that:\n"
        "- Start conversationally (no 'Dear subscriber')\n"
        "- Tease the key value without giving everything away\n"
        "- End with a 'Read more →' invite\n\n"
        "Return only the email body, no subject line."
    ),
}


class ContentRepurposer(BaseAgent):
    async def run(self, site_id: str) -> list[Alert]:
        result = await self.db.execute(select(Site).where(Site.id == site_id))
        site = result.scalar_one_or_none()
        if not site:
            return []

        posts_result = await self.db.execute(
            select(ContentPost)
            .where(ContentPost.site_id == site_id, ContentPost.health_score >= 50)
            .order_by(ContentPost.health_score.desc())
            .limit(5)
        )
        posts = posts_result.scalars().all()

        for post in posts:
            # Skip if already repurposed
            existing = await self.db.execute(
                select(Variant).where(Variant.content_post_id == post.id).limit(1)
            )
            if existing.scalar_one_or_none():
                continue

            for channel, prompt_tpl in _PROMPTS.items():
                try:
                    content = await asyncio.wait_for(
                        ai_engine.analyze(prompt_tpl.format(title=post.title, url=post.url)),
                        timeout=30,
                    )
                    if not content.strip():
                        continue

                    variant = Variant(
                        content_post_id=post.id,
                        channel=channel,
                        content=content,
                        metadata_={"post_title": post.title, "post_url": post.url},
                        status="pending",
                    )
                    self.db.add(variant)
                    await self.db.flush()

                    review = ReviewItem(
                        agent="autopilot",
                        action_type="social_post",
                        payload={
                            "channel": channel,
                            "post_title": post.title,
                            "post_url": post.url,
                            "content_preview": content[:200],
                            "variant_id": variant.id,
                        },
                        status="pending",
                        site_id=site_id,
                    )
                    self.db.add(review)

                except Exception:
                    continue  # If AI fails, skip this variant

        await self.db.flush()
        return []
