"""SEO Analyzer — content-based SEO issues + real GSC ranking opportunities."""
import html as _html
import logging
import re

from sqlalchemy import delete, select

from app.agents.base import BaseAgent
from app.agents.wp_fetch import get_pages_cached, get_posts_cached
from app.database.models import Alert, ContentPost, Site

logger = logging.getLogger(__name__)


class SEOAnalyzer(BaseAgent):
    async def run(self, site_id: str) -> list[Alert]:
        result = await self.db.execute(select(Site).where(Site.id == site_id))
        site = result.scalar_one_or_none()
        if not site:
            return []

        try:
            wp_posts = await get_posts_cached(site.url, site.api_key)
            wp_pages = await get_pages_cached(site.url, site.api_key)
        except Exception as exc:
            logger.warning("SEOAnalyzer skipped — could not fetch posts from %s: %s", site.url, exc)
            return []
        wp_posts = wp_posts + wp_pages

        await self.db.execute(
            delete(Alert).where(
                Alert.site_id == site_id,
                Alert.agent == "optimizer",
                Alert.type.like("seo_%"),
            )
        )

        alerts: list[Alert] = []

        # Load all ContentPost rows for this site once — one query, not one per post
        posts_r = await self.db.execute(
            select(ContentPost).where(ContentPost.site_id == site_id)
        )
        db_posts_by_wp_id = {p.wp_post_id: p for p in posts_r.scalars().all()}

        # ── Content-based analysis (always runs) ──────────────────────────────
        for wp_post in wp_posts:
            wp_id = wp_post.get("id")
            if not wp_id:
                continue

            post = db_posts_by_wp_id.get(wp_id)
            if not post:
                continue

            title_raw = wp_post.get("title", {})
            title = title_raw.get("rendered", "") if isinstance(title_raw, dict) else str(title_raw)
            title = _html.unescape(re.sub(r"<[^>]+>", "", title)).strip()

            content_raw = wp_post.get("content", {})
            html = content_raw.get("rendered", "") if isinstance(content_raw, dict) else ""
            word_count = len(re.sub(r"<[^>]+>", " ", html).split())

            if len(title) < 30:
                alerts.append(await self.create_alert(
                    site_id=site_id, agent="optimizer", severity="info",
                    type_="seo_title_short",
                    title=f"SEO: Title too short — {title[:50]}",
                    description=f'Title is {len(title)} chars. Expand to 30–60 for better CTR. Current: "{title}"',
                    metadata={"page_title": title, "page_url": post.url, "word_count": word_count},
                ))
            elif len(title) > 70:
                alerts.append(await self.create_alert(
                    site_id=site_id, agent="optimizer", severity="info",
                    type_="seo_title_long",
                    title=f"SEO: Title too long — {title[:50]}",
                    description=f"Title is {len(title)} chars, will be truncated in SERPs. Aim for 30–60.",
                    metadata={"page_title": title, "page_url": post.url, "word_count": word_count},
                ))

            if word_count < 300:
                alerts.append(await self.create_alert(
                    site_id=site_id, agent="optimizer", severity="warning",
                    type_="seo_thin_content",
                    title=f"SEO: Thin content ({word_count} words) — {title[:50]}",
                    description=f"Only {word_count} words. Expand to 800+ to rank competitively.",
                    metadata={"page_title": title, "page_url": post.url, "word_count": word_count},
                ))

            has_featured = int(wp_post.get("featured_media") or 0) > 0
            if html.lower().count("<img") == 0 and not has_featured:
                alerts.append(await self.create_alert(
                    site_id=site_id, agent="optimizer", severity="info",
                    type_="seo_no_images",
                    title=f"SEO: No images — {title[:50]}",
                    description="No images found. Add at least one with descriptive alt text.",
                    metadata={"page_title": title, "page_url": post.url, "word_count": word_count},
                ))

        # ── GSC ranking opportunities (only when Google is connected) ─────────
        try:
            from app.api.auth import get_google_token
            from app.connectors.search_console import SearchConsoleConnector
            from app.database.models import SiteConfig

            token = await get_google_token(self.db)
            if token:
                cfg_r = await self.db.execute(
                    select(SiteConfig).where(SiteConfig.site_id == site_id)
                )
                cfg = cfg_r.scalar_one_or_none()
                gsc_url = (cfg.gsc_site_url if cfg else None) or site.url

                gsc = SearchConsoleConnector(token.access_token)
                opportunities = await gsc.get_opportunity_queries(gsc_url)

                for opp in opportunities[:10]:
                    alerts.append(await self.create_alert(
                        site_id=site_id, agent="optimizer", severity="info",
                        type_="seo_ranking_opportunity",
                        title=f"SEO opportunity: \"{opp['query']}\" at position {opp['position']}",
                        description=(
                            f"{opp['impressions']} impressions, {opp['clicks']} clicks "
                            f"({opp['ctr']}% CTR). Optimise this page to reach the top 3."
                        ),
                        metadata={
                            "query": opp["query"],
                            "position": opp["position"],
                            "impressions": opp["impressions"],
                            "clicks": opp["clicks"],
                            "ctr": opp["ctr"],
                            "estimated_traffic_gain": int(opp["impressions"] * 0.3),
                        },
                    ))
        except Exception:
            pass  # GSC is optional — content analysis always runs

        return alerts
