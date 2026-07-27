"""Internal Linker — suggests internal links between semantically related posts.

Candidates are found via an inverted keyword index (keyword -> post ids)
rather than comparing every post against every other post: comparing all
pairs directly is O(n^2), which is fine for a few hundred posts but
infeasible for an enterprise site with thousands. The index means a post
only gets compared against posts it already shares at least one keyword
with, so cost scales with actual keyword overlap instead of site size.
"""
import re

from sqlalchemy import delete, select

from app.agents.base import BaseAgent
from app.database.models import Alert, ContentPost, Site

_STOP_WORDS = {
    "the", "and", "for", "how", "with", "your", "from", "this", "that",
    "what", "when", "where", "which", "have", "will", "been", "they",
    "their", "about", "into", "more", "also", "some", "than",
}


def _keywords(title: str) -> set[str]:
    words = re.findall(r"\b[a-z]{4,}\b", title.lower())
    return {w for w in words if w not in _STOP_WORDS}


class InternalLinker(BaseAgent):
    async def run(self, site_id: str) -> list[Alert]:
        result = await self.db.execute(select(Site).where(Site.id == site_id))
        site = result.scalar_one_or_none()
        if not site:
            return []

        posts_result = await self.db.execute(
            select(ContentPost).where(ContentPost.site_id == site_id)
        )
        posts = posts_result.scalars().all()

        if len(posts) < 2:
            return []

        # Clear old internal-link suggestions for this site
        await self.db.execute(
            delete(Alert).where(
                Alert.site_id == site_id,
                Alert.agent == "optimizer",
                Alert.type == "internal_link",
            )
        )

        keywords_by_post: dict[str, set[str]] = {}
        posts_by_keyword: dict[str, list[str]] = {}
        for post in posts:
            kw = _keywords(post.title)
            keywords_by_post[post.id] = kw
            for word in kw:
                posts_by_keyword.setdefault(word, []).append(post.id)

        posts_by_id = {p.id: p for p in posts}
        alerts: list[Alert] = []
        seen_pairs: set[frozenset[str]] = set()

        for post in posts:
            kw = keywords_by_post[post.id]
            if not kw:
                continue

            candidate_ids = {pid for word in kw for pid in posts_by_keyword[word]} - {post.id}
            for other_id in candidate_ids:
                pair = frozenset([post.id, other_id])
                if pair in seen_pairs:
                    continue

                shared = kw & keywords_by_post[other_id]
                if len(shared) >= 2:
                    seen_pairs.add(pair)
                    other = posts_by_id[other_id]
                    anchor = max(shared, key=len)
                    alert = await self.create_alert(
                        site_id=site_id,
                        agent="optimizer",
                        severity="info",
                        type_="internal_link",
                        title=f"Link opportunity: {post.title[:50]}",
                        description=(
                            f'Add a link in "{other.title}" pointing to "{post.title}" '
                            f'using anchor text "{anchor}".'
                        ),
                        metadata={
                            "source_title": other.title,
                            "source_url": other.url,
                            "target_title": post.title,
                            "target_url": post.url,
                            "anchor_text": anchor,
                            "shared_keywords": list(shared),
                        },
                    )
                    alerts.append(alert)

                    if len(alerts) >= 20:
                        return alerts

        return alerts
