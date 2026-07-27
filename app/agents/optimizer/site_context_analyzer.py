"""
SiteContextAnalyzer — understands what a WordPress site does before generating recommendations.

Fetches:
  - WP REST API root (name, tagline/description) — public
  - Up to 30 most recent post titles + excerpts — public
  - (Optional) categories taxonomy — public

Feeds this to the AI to produce a structured site profile:
  business_type, industry, primary_offerings, target_audience,
  brand_tone, main_topics, location_focus, summary

This profile is stored on Site.site_context and injected into every
per-post AI recommendation prompt so advice is personalised.
"""
import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.engine import ai_engine
from app.connectors.wordpress import WordPressConnector
from app.database.models import ContentPost, Site

logger = logging.getLogger(__name__)

_SCHEMA_HINT = """{
  "business_type": "service_provider|product|ecommerce|blog|news|agency|personal|other",
  "industry": "e.g. digital marketing, healthcare, finance, technology, education, legal, real estate…",
  "primary_offerings": ["up to 5 specific products or services"],
  "target_audience": "one-sentence description of the ideal customer/reader",
  "brand_tone": "professional|casual|technical|educational|conversational|authoritative",
  "main_topics": ["up to 6 recurring content themes or keywords"],
  "location_focus": "local|national|global",
  "summary": "2–3 sentence plain-English summary of what this site does and who it serves"
}"""


async def analyze_site_context(site_id: str, db: AsyncSession) -> dict:
    """
    Run site context analysis for a single site.
    Updates site.site_context and site.site_context_analyzed_at.
    Returns the context dict (empty dict on failure).
    """
    result = await db.execute(select(Site).where(Site.id == site_id))
    site = result.scalar_one_or_none()
    if not site:
        return {}

    # ── Gather signals ────────────────────────────────────────────────────

    # 1. WordPress root endpoint (name + tagline)
    wp = WordPressConnector(site.url, site.api_key)
    try:
        site_info = await wp.get_site_info()
    except Exception:
        site_info = {"name": site.name, "description": "", "url": site.url}
    finally:
        await wp.close()

    wp_name = site_info.get("name") or site.name
    wp_tagline = site_info.get("description") or ""

    # 2. Post titles + excerpts from DB (already synced, no extra HTTP call)
    posts_result = await db.execute(
        select(ContentPost.title, ContentPost.issues)
        .where(ContentPost.site_id == site_id)
        .order_by(ContentPost.created_at.desc())
        .limit(30)
    )
    posts = posts_result.all()
    post_titles = [row[0] for row in posts if row[0]]

    if not post_titles:
        logger.warning("SiteContextAnalyzer: no posts found for site %s — skipping", site_id)
        return {}

    # ── Build prompt ──────────────────────────────────────────────────────
    titles_block = "\n".join(f"  - {t}" for t in post_titles[:25])

    prompt = f"""You are analysing a WordPress website to build a business context profile.

Site name: {wp_name}
Site URL: {site.url}
WordPress tagline: {wp_tagline or "(not set)"}

Sample of recent post titles:
{titles_block}

Based ONLY on the information above, infer the site's business context.
Be specific — if you can identify a niche (e.g. "WordPress speed optimisation agency" rather than "tech"), do so.
If something cannot be determined, use a reasonable generic value rather than leaving it empty.
"""

    try:
        context = await ai_engine.generate_json(prompt, schema_hint=_SCHEMA_HINT, max_tokens=700)
    except Exception as exc:
        logger.error("SiteContextAnalyzer: AI call failed for site %s: %s", site_id, exc)
        return {}

    if not context or not isinstance(context, dict):
        return {}

    context["analyzed_at"] = datetime.now(timezone.utc).isoformat()
    context["wp_name"] = wp_name
    context["wp_tagline"] = wp_tagline

    site.site_context = context
    site.site_context_analyzed_at = datetime.now(timezone.utc)

    logger.info(
        "SiteContextAnalyzer: site %s identified as %s / %s",
        site_id,
        context.get("business_type", "?"),
        context.get("industry", "?"),
    )
    return context
