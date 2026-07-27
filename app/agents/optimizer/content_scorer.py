"""Content Scorer — deep analysis: word count, images, links, freshness, title, headings, schema, meta description, AI recs."""
import asyncio
import hashlib
import html as _html
import json
import logging
import re
from datetime import datetime, timezone
from urllib.parse import urlparse

import httpx
from sqlalchemy import select

from app.agents.base import BaseAgent
from app.agents.wp_fetch import get_pages_cached, get_posts_cached
from app.ai.engine import FAST_MODEL, ai_engine
from app.config import settings
from app.connectors.wordpress import parse_wp_datetime
from app.database.models import Alert, ContentPost, Site

logger = logging.getLogger(__name__)


def _empty_signals() -> dict:
    """h1_count=None means 'could not verify' — callers must not treat it as zero."""
    return {"schema_types": [], "h1_count": None}


async def _fetch_page_signals(url: str) -> dict:
    """
    Fetch the live post URL ONCE and extract full-page signals:

    - schema_types: every @type found in ALL <script type="application/ld+json">
      blocks on the rendered page (head, footer, sidebars, plugin injections)
    - h1_count: H1 tags on the rendered page — themes usually render the post
      title as H1 in the template, OUTSIDE the REST API's content.rendered, so
      a body-only count of 0 needs this live confirmation before flagging.

    Returns {"schema_types": [], "h1_count": None} on any network error.
    """
    if not url or not url.startswith("http"):
        return _empty_signals()
    try:
        async with httpx.AsyncClient(timeout=6, follow_redirects=True) as client:
            resp = await client.get(url, headers={"User-Agent": "WPCommandCenter/1.0 (schema-audit)"})
            resp.raise_for_status()
            page_html = resp.text
    except Exception:
        return _empty_signals()

    types: list[str] = []
    ld_blocks = re.findall(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        page_html, re.DOTALL | re.I,
    )
    for raw in ld_blocks:
        try:
            data = json.loads(raw.strip())
            items: list = data if isinstance(data, list) else [data]
            for item in items:
                if not isinstance(item, dict):
                    continue
                _collect_types(item, types)
        except Exception:
            pass

    return {
        "schema_types": list(dict.fromkeys(types)),  # deduplicate, preserve order
        "h1_count": len(re.findall(r"<h1[\s>]", page_html, re.I)),
    }


def _collect_types(node: dict, out: list[str]) -> None:
    """Recursively collect @type values from a JSON-LD node and its @graph."""
    t = node.get("@type")
    if isinstance(t, str) and t:
        out.append(t)
    elif isinstance(t, list):
        out.extend(v for v in t if isinstance(v, str) and v)
    for child in node.get("@graph", []):
        if isinstance(child, dict):
            _collect_types(child, out)


def _local_schema_types(wp_post: dict) -> list[str]:
    """Schema @types already present in the REST payload (body JSON-LD + Yoast graph).

    When this finds anything, the live-page fetch can be skipped entirely.
    """
    types: list[str] = []

    content_raw = wp_post.get("content", {})
    body_html = content_raw.get("rendered", "") if isinstance(content_raw, dict) else ""
    for raw in re.findall(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        body_html, re.DOTALL | re.I,
    ):
        try:
            data = json.loads(raw.strip())
            for item in data if isinstance(data, list) else [data]:
                if isinstance(item, dict):
                    _collect_types(item, types)
        except Exception:
            pass

    yoast_graph = ((wp_post.get("yoast_head_json") or {}).get("schema") or {}).get("@graph", [])
    for node in yoast_graph:
        if isinstance(node, dict):
            _collect_types(node, types)

    return list(dict.fromkeys(types))


def _extract_schema(html: str, wp_post: dict) -> dict:
    """
    Extract structured data from multiple sources, in priority order:
    1. JSON-LD <script> blocks embedded in the content HTML
    2. yoast_head_json (Yoast SEO plugin, if installed)
    3. WP REST API native fields (excerpt, date, modified)
    Returns a flat dict with normalised keys.
    """
    schema: dict = {}

    # ── 1. JSON-LD blocks in content HTML ────────────────────────────────
    ld_blocks = re.findall(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.DOTALL | re.I,
    )
    for raw in ld_blocks:
        try:
            data = json.loads(raw.strip())
            items: list = data if isinstance(data, list) else [data]
            for item in items:
                if not isinstance(item, dict):
                    continue
                # Top-level Article/BlogPosting
                if item.get("@type") in ("Article", "BlogPosting", "NewsArticle", "WebPage"):
                    for k, v in item.items():
                        schema.setdefault(k, v)
                # @graph array (common with Yoast schema output)
                for node in item.get("@graph", []):
                    if isinstance(node, dict) and node.get("@type") in (
                        "Article", "BlogPosting", "NewsArticle", "WebPage"
                    ):
                        for k, v in node.items():
                            schema.setdefault(k, v)
        except Exception:
            pass

    # ── 2. yoast_head_json (Yoast SEO plugin) ────────────────────────────
    yoast = wp_post.get("yoast_head_json") or {}
    if yoast:
        schema.setdefault("description", yoast.get("description") or yoast.get("og_description"))
        schema.setdefault("seo_title", yoast.get("title") or yoast.get("og_title"))
        schema.setdefault("dateModified", yoast.get("article_modified_time"))
        schema.setdefault("datePublished", yoast.get("article_published_time"))
        schema.setdefault("og_image", yoast.get("og_image"))  # list of {url, width, height}
        # Yoast also embeds a full schema graph in its head JSON
        for node in (yoast.get("schema") or {}).get("@graph", []):
            if isinstance(node, dict) and node.get("@type") in (
                "Article", "BlogPosting", "NewsArticle", "WebPage"
            ):
                for k, v in node.items():
                    schema.setdefault(k, v)

    # ── 3. WP REST API native fields ─────────────────────────────────────
    schema.setdefault("dateModified", wp_post.get("modified_gmt") or wp_post.get("modified"))
    schema.setdefault("datePublished", wp_post.get("date_gmt") or wp_post.get("date"))

    excerpt_raw = wp_post.get("excerpt", {})
    excerpt_html = excerpt_raw.get("rendered", "") if isinstance(excerpt_raw, dict) else str(excerpt_raw)
    excerpt_text = re.sub(r"<[^>]+>", " ", excerpt_html).strip()
    if excerpt_text:
        schema.setdefault("description", excerpt_text)

    return schema


def _faq_content_detected(html: str) -> bool:
    """At least 2 of 5 heuristic signals suggest this post has FAQ-style content.

    Shared by `_analyze()` (to decide whether to flag a missing FAQPage
    schema) and `ContentScorer.run()` (to decide whether a post needs a live
    full-page crawl before trusting that the schema is really missing).
    """
    signals = [
        bool(re.search(r'\bfrequently\s+asked\s+questions?\b', html, re.I)),
        bool(re.search(r'\bfaq\b', html, re.I)),
        bool(re.search(r'<details[^>]*>', html, re.I)),   # accordion/details elements
        len(re.findall(r'<h[23][^>]*>[^<]*\?[^<]*</h[23]>', html, re.I)) >= 2,  # 2+ heading questions
        len(re.findall(r'<strong[^>]*>[^<]*\?[^<]*</strong>', html, re.I)) >= 3,  # 3+ bold questions
    ]
    return sum(signals) >= 2


def _needs_live_fetch(local_types: list[str], body_html: str) -> bool:
    """Decide whether a post's local (REST body + Yoast) signals can be trusted.

    A live full-page crawl is required when:
      - no schema was found locally at all
      - the body has no H1 (themes often render the title H1 in the
        template, outside the REST content field)
      - the body looks like FAQ content but no local source reports a
        FAQPage type — many FAQ plugins/blocks inject their schema straight
        into the rendered page (footer, widget, custom HTML) rather than
        through Yoast's graph or the REST content field, so "some schema
        found locally" must NOT be read as "FAQ schema was ruled out".
    """
    body_has_h1 = bool(re.search(r"<h1[\s>]", body_html, re.I))
    faq_unconfirmed = "FAQPage" not in local_types and _faq_content_detected(body_html)
    return not local_types or not body_has_h1 or faq_unconfirmed


def _analyze(
    wp_post: dict,
    site_url: str,
    extra_schema_types: list[str] | None = None,
    live_h1_count: int | None = None,
) -> tuple[int, list[str], dict, int, int]:
    """
    Deep analysis of a WP post.
    Returns (health_score 0-100, issues, score_breakdown, word_count, reading_time_minutes).
    """
    score = 0
    issues: list[str] = []
    breakdown: dict = {}

    content_raw = wp_post.get("content", {})
    html = content_raw.get("rendered", "") if isinstance(content_raw, dict) else ""
    text = re.sub(r"<[^>]+>", " ", html).strip()
    word_count = len(text.split()) if text else 0
    reading_time = max(1, word_count // 200)

    # Extract schema data once for reuse
    schema = _extract_schema(html, wp_post)

    # ── Word count — 25 pts ──────────────────────────────────────────────
    if word_count >= 1000:
        wc_score, wc_status = 25, "good"
        wc_detail = f"{word_count:,} words"
    elif word_count >= 500:
        wc_score, wc_status = 15, "warning"
        wc_detail = f"{word_count:,} words — aim for 1,000+"
        issues.append(f"Short content ({word_count:,} words, aim for 1,000+)")
    elif word_count >= 200:
        wc_score, wc_status = 5, "warning"
        wc_detail = f"{word_count:,} words — very short"
        issues.append(f"Very short content ({word_count:,} words)")
    else:
        wc_score, wc_status = 0, "critical"
        wc_detail = f"{word_count:,} words — too short"
        issues.append(f"Content too short ({word_count:,} words)")

    score += wc_score
    breakdown["word_count"] = {
        "score": wc_score, "max": 25, "value": word_count,
        "reading_time_minutes": reading_time, "status": wc_status, "detail": wc_detail,
    }

    # ── Images — 20 pts ──────────────────────────────────────────────────
    inline_count = html.lower().count("<img")
    has_featured = int(wp_post.get("featured_media") or 0) > 0

    # Also count Open Graph / schema images as a signal (not as inline count)
    og_images = schema.get("og_image") or []
    has_og_image = bool(og_images) if isinstance(og_images, list) else bool(og_images)

    total_images = inline_count + (1 if has_featured else 0)

    if total_images >= 2:
        img_score, img_status = 20, "good"
        if has_featured and inline_count >= 1:
            img_detail = f"Featured image + {inline_count} inline image{'s' if inline_count > 1 else ''}"
        else:
            img_detail = f"{inline_count} inline images"
    elif total_images == 1:
        img_score, img_status = 10, "warning"
        if has_featured and inline_count == 0:
            img_detail = "Featured image only — add inline images to the article body"
            issues.append("Featured image only — add inline images to the article body")
        else:
            img_detail = "Only 1 inline image — add more visuals"
            issues.append("Only 1 image — add more visuals")
    else:
        img_score, img_status = 0, "critical"
        img_detail = "No images — add a featured image and inline visuals"
        issues.append("No images — add a featured image and inline visuals")

    score += img_score
    breakdown["images"] = {
        "score": img_score, "max": 20,
        "inline_count": inline_count, "has_featured": has_featured,
        "has_og_image": has_og_image, "total": total_images,
        "status": img_status, "detail": img_detail,
    }

    # ── Links — 20 pts ───────────────────────────────────────────────────
    site_domain = urlparse(site_url).netloc if site_url else ""
    hrefs = re.findall(r'href=["\']([^"\']+)["\']', html)
    internal_count = sum(
        1 for h in hrefs
        if (site_domain and site_domain in h) or h.startswith("/")
    )
    external_count = sum(
        1 for h in hrefs
        if h.startswith("http") and (not site_domain or site_domain not in h)
    )
    total_links = internal_count + external_count

    if total_links >= 3:
        link_score, link_status = 20, "good"
        link_detail = f"{total_links} links ({internal_count} internal, {external_count} external)"
    elif total_links >= 1:
        link_score, link_status = 10, "warning"
        link_detail = f"{total_links} link{'s' if total_links > 1 else ''} — add more internal links"
        issues.append(f"Only {total_links} link(s) — add more internal links")
    else:
        link_score, link_status = 0, "critical"
        link_detail = "No links — add internal and external links"
        issues.append("No links — add internal and external links")

    score += link_score
    breakdown["links"] = {
        "score": link_score, "max": 20,
        "internal_count": internal_count, "external_count": external_count, "total": total_links,
        "status": link_status, "detail": link_detail,
    }

    # ── Freshness — 25 pts ───────────────────────────────────────────────
    # Use schema dateModified (may come from Yoast/JSON-LD, more accurate than WP field)
    modified_str = schema.get("dateModified") or wp_post.get("modified")
    age_days = None
    if modified_str:
        try:
            mod_dt = datetime.fromisoformat(modified_str.replace("Z", "+00:00"))
            age_days = (datetime.now(timezone.utc) - mod_dt).days
            if age_days <= 180:
                fresh_score, fresh_status = 25, "good"
                fresh_detail = f"Updated {age_days} days ago"
            elif age_days <= 365:
                fresh_score, fresh_status = 15, "warning"
                fresh_detail = f"Last updated {age_days} days ago — consider refreshing"
                issues.append(f"Last updated {age_days} days ago — consider refreshing")
            else:
                fresh_score, fresh_status = 0, "critical"
                fresh_detail = f"Stale — last updated {age_days} days ago"
                issues.append(f"Stale content — last updated {age_days} days ago")
        except Exception:
            fresh_score, fresh_status = 10, "warning"
            fresh_detail = "Modified date unavailable"
    else:
        fresh_score, fresh_status = 10, "warning"
        fresh_detail = "No modification date"

    score += fresh_score
    breakdown["freshness"] = {
        "score": fresh_score, "max": 25,
        "age_days": age_days, "status": fresh_status, "detail": fresh_detail,
    }

    # ── Title — 10 pts ───────────────────────────────────────────────────
    title_raw = wp_post.get("title", {})
    title = title_raw.get("rendered", "") if isinstance(title_raw, dict) else str(title_raw)
    title = _html.unescape(re.sub(r"<[^>]+>", "", title)).strip()
    title_len = len(title)

    if 30 <= title_len <= 60:
        title_score, title_status = 10, "good"
        title_detail = f"{title_len} characters (optimal 30–60)"
    elif title_len < 30:
        title_score, title_status = 0, "warning"
        title_detail = f"{title_len} characters — too short (aim for 30–60)"
        issues.append(f"Title too short ({title_len} chars, aim for 30–60)")
    else:
        title_score, title_status = 0, "warning"
        title_detail = f"{title_len} characters — too long (aim for 30–60)"
        issues.append(f"Title too long ({title_len} chars, aim for 30–60)")

    score += title_score
    breakdown["title"] = {
        "score": title_score, "max": 10,
        "length": title_len, "status": title_status, "detail": title_detail,
    }

    # ── Headings — deep analysis ─────────────────────────────────────────
    body_h1_count = len(re.findall(r"<h1[^>]*>", html, re.I))
    h2_count = len(re.findall(r"<h2[^>]*>", html, re.I))
    h3_count = len(re.findall(r"<h3[^>]*>", html, re.I))
    h4_count = len(re.findall(r"<h4[^>]*>", html, re.I))

    # The REST API's content.rendered only contains the post BODY — themes
    # normally render the H1 title in the template around it. When the body
    # has no H1, use the live-page crawl (when available) as the verdict:
    # H1 found there → fine (theme renders it); confirmed 0 → real issue.
    h1_count = body_h1_count
    h1_source = "post_content"
    h1_confirmed_missing = False
    if body_h1_count == 0 and live_h1_count is not None:
        h1_count = live_h1_count
        h1_source = "live_page"
        h1_confirmed_missing = live_h1_count == 0

    # Check heading hierarchy — extract level sequence and find violations
    heading_tags = re.findall(r"<h([1-6])[^>]*>", html, re.I)
    heading_levels = [int(t) for t in heading_tags]
    hierarchy_issues: list[str] = []

    if h1_count > 1:
        where = "page" if h1_source == "live_page" else "article body"
        hierarchy_issues.append(f"Multiple H1 tags ({h1_count}) — the {where} should have at most one H1")
        issues.append(f"Multiple H1 headings ({h1_count}) — keep only one H1 per page")
    elif h1_confirmed_missing:
        hierarchy_issues.append("No H1 anywhere on the live page (crawled to confirm)")
        issues.append("Missing H1 — the rendered page has no H1 heading")

    # Detect level skips: e.g. H2 → H4 without H3 in between
    for i in range(1, len(heading_levels)):
        prev, curr = heading_levels[i - 1], heading_levels[i]
        if curr > prev + 1:
            hierarchy_issues.append(f"Heading jump: H{prev} → H{curr} (skipped H{prev + 1})")
    if hierarchy_issues and len(hierarchy_issues) <= 3:
        issues.append(f"Heading hierarchy issue: {hierarchy_issues[0]}")

    h1_label = f"H1×{h1_count}" + (" (theme)" if h1_source == "live_page" and h1_count > 0 else "")

    if h1_confirmed_missing:
        heading_detail = "No H1 on the rendered page — add one (usually the post title)"
        heading_status = "warning"
    elif h2_count == 0 and word_count >= 300:
        heading_detail = "No H2 headings — add structure with subheadings"
        issues.append("No H2 headings — structure the article with subheadings")
        heading_status = "warning"
    elif hierarchy_issues:
        heading_detail = f"{h1_label}, H2×{h2_count}, H3×{h3_count} — hierarchy issues found"
        heading_status = "warning"
    elif h2_count > 0:
        heading_detail = (
            f"{h1_label}, H2×{h2_count}, H3×{h3_count}"
            + (f", H4×{h4_count}" if h4_count > 0 else "")
        )
        heading_status = "good"
    else:
        heading_detail = "No headings (short content)"
        heading_status = "info"

    breakdown["headings"] = {
        "h1_count": h1_count,
        "h1_source": h1_source,
        "h2_count": h2_count,
        "h3_count": h3_count,
        "h4_count": h4_count,
        "hierarchy_issues": hierarchy_issues,
        "status": heading_status,
        "detail": heading_detail,
    }

    # ── Meta description — informational (from schema / Yoast / excerpt) ─
    meta_desc = (schema.get("description") or "").strip()
    # Strip any remaining HTML from excerpt
    meta_desc = re.sub(r"<[^>]+>", "", meta_desc).strip()
    meta_len = len(meta_desc)

    if 120 <= meta_len <= 160:
        meta_status = "good"
        meta_detail = f"{meta_len} characters — optimal (120–160)"
    elif 1 <= meta_len < 120:
        meta_status = "warning"
        meta_detail = f"{meta_len} characters — too short (aim for 120–160)"
        issues.append("Meta description too short — expand to 120–160 characters")
    elif meta_len > 160:
        meta_status = "warning"
        meta_detail = f"{meta_len} characters — may be truncated in search results (aim ≤160)"
        issues.append("Meta description too long — keep under 160 characters")
    else:
        meta_status = "critical"
        meta_detail = "No meta description — set via Yoast SEO or the post excerpt"
        issues.append("No meta description — set via Yoast SEO or the post excerpt")

    seo_title_raw = (schema.get("seo_title") or "").strip()
    seo_title = re.sub(r"<[^>]+>", "", seo_title_raw).strip()

    breakdown["meta_description"] = {
        "status": meta_status, "detail": meta_detail,
        "length": meta_len,
        "meta_title": seo_title or None,
        "meta_description": meta_desc or None,
        "preview": (meta_desc[:120] + "…") if meta_len > 120 else meta_desc,
        "source": "yoast" if wp_post.get("yoast_head_json") else ("excerpt" if meta_len > 0 else "none"),
    }

    # ── Publish history — informational ──────────────────────────────────
    pub_str = schema.get("datePublished")
    pub_age_days = None
    never_updated = False
    if pub_str and modified_str:
        try:
            pub_dt = datetime.fromisoformat(pub_str.replace("Z", "+00:00"))
            mod_dt = datetime.fromisoformat(modified_str.replace("Z", "+00:00"))
            pub_age_days = (datetime.now(timezone.utc) - pub_dt).days
            never_updated = abs((mod_dt - pub_dt).total_seconds()) < 86400  # same day = never updated
            if never_updated and pub_age_days > 365:
                issues.append(
                    f"Published {pub_age_days // 365} year(s) ago and never refreshed — update to stay relevant"
                )
        except Exception:
            pass

    breakdown["publish_history"] = {
        "published_str": pub_str,
        "modified_str": modified_str,
        "pub_age_days": pub_age_days,
        "never_updated": never_updated,
        "detail": (
            f"Never refreshed since publishing {pub_age_days // 365}y ago" if never_updated and pub_age_days and pub_age_days > 365
            else f"Published {pub_age_days} days ago" if pub_age_days else "Publication date unknown"
        ),
    }

    # ── Schema markup — informational ────────────────────────────────────
    schema_type = schema.get("@type") or ""
    has_structured_data = bool(schema_type or schema.get("description"))

    # ── Collect ALL schema types across every source ──────────────────────
    # Source 1: JSON-LD blocks inside post body (content.rendered)
    body_types: list[str] = []
    ld_blocks_raw = re.findall(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.DOTALL | re.I,
    )
    for raw in ld_blocks_raw:
        try:
            data = json.loads(raw.strip())
            items: list = data if isinstance(data, list) else [data]
            for item in items:
                if isinstance(item, dict):
                    _collect_types(item, body_types)
        except Exception:
            pass

    # Source 2: Yoast head JSON @graph (head-injected schema)
    yoast_types: list[str] = []
    for node in (wp_post.get("yoast_head_json") or {}).get("schema", {}).get("@graph", []):
        if isinstance(node, dict):
            _collect_types(node, yoast_types)

    # Source 3: full page HTML fetch result passed in from caller
    page_types: list[str] = list(extra_schema_types or [])

    # Merge — deduplicate while preserving order; page_types is authoritative
    # (it includes everything: head, body, footer, plugin output)
    seen: set[str] = set()
    all_schema_types: list[str] = []
    for t in (page_types or (body_types + yoast_types)):
        if t not in seen:
            seen.add(t)
            all_schema_types.append(t)

    # If page fetch wasn't done, still merge body + yoast
    if not page_types:
        for t in yoast_types:
            if t not in seen:
                seen.add(t)
                all_schema_types.append(t)

    has_faq_schema = "FAQPage" in all_schema_types
    has_structured_data = bool(all_schema_types) or has_structured_data

    # Detect FAQ-like content in the article body
    faq_content_detected = _faq_content_detected(html)

    if faq_content_detected and not has_faq_schema:
        issues.append("FAQ content detected but no FAQPage schema — add FAQ schema to boost rich results")
        faq_recommendation = "missing"
    elif faq_content_detected and has_faq_schema:
        faq_recommendation = "present"
    else:
        faq_recommendation = "not_applicable"

    schema_detail_parts: list[str] = []
    if all_schema_types:
        schema_detail_parts.append(f"{len(all_schema_types)} schema type(s) found")
        if page_types:
            schema_detail_parts.append("scanned full page")
    elif has_structured_data:
        schema_detail_parts.append("Partial schema detected")
    else:
        schema_detail_parts.append("No structured data found")

    if has_faq_schema:
        schema_detail_parts.append("FAQPage schema ✓")
    elif faq_content_detected:
        schema_detail_parts.append("FAQ content found — schema missing")

    # Primary type for display: prefer content-level types, then yoast, then first found
    primary_type = (
        next((t for t in all_schema_types if t in ("Article", "BlogPosting", "NewsArticle", "WebPage", "FAQPage")), None)
        or (all_schema_types[0] if all_schema_types else "none")
    )

    breakdown["schema_markup"] = {
        "has_structured_data": has_structured_data,
        "type": primary_type,
        "all_types": all_schema_types,  # already ordered/deduplicated
        "sources": {
            "body": body_types,
            "yoast": yoast_types,
            "full_page": page_types,
        },
        "full_page_scanned": bool(page_types),
        "has_faq_schema": has_faq_schema,
        "faq_content_detected": faq_content_detected,
        "faq_recommendation": faq_recommendation,
        "detail": " · ".join(schema_detail_parts),
    }

    return min(score, 100), issues, breakdown, word_count, reading_time


async def _generate_ai_recommendation(
    title: str,
    score: int,
    word_count: int,
    issues: list[str],
    breakdown: dict,
    site_context: dict | None = None,
) -> str | None:
    """
    Generate 3 personalised, actionable AI recommendations for the post.
    site_context (from SiteContextAnalyzer) makes advice specific to the
    site's business type, industry, audience and tone.

    Return contract — callers must distinguish these to avoid displaying a
    stale recommendation after the underlying issue is fixed:
      - ""   the post was analyzed and is genuinely clean — no rec needed.
             Callers should CLEAR any previously stored recommendation.
      - str  newline-joined recommendations from a successful AI call.
      - None generation failed (network/API error) — the analysis is
             inconclusive, so callers must leave any existing text alone
             rather than erase advice that may still be accurate.
    """
    if not issues and score >= 80:
        return ""
    try:
        # ── Site context block ────────────────────────────────────────────
        site_ctx_block = ""
        if site_context and site_context.get("summary"):
            site_ctx_block = (
                "SITE CONTEXT (use this to make recommendations specific and relevant):\n"
                f"  Business type : {site_context.get('business_type', 'unknown')}\n"
                f"  Industry      : {site_context.get('industry', 'unknown')}\n"
                f"  Offerings     : {', '.join(site_context.get('primary_offerings', []))}\n"
                f"  Target audience: {site_context.get('target_audience', 'unknown')}\n"
                f"  Brand tone    : {site_context.get('brand_tone', 'professional')}\n"
                f"  Summary       : {site_context.get('summary', '')}\n\n"
            )

        # ── Post analysis block ───────────────────────────────────────────
        heading_ctx = ""
        if "headings" in breakdown:
            hd = breakdown["headings"]
            heading_ctx = (
                f"  Heading structure: H1×{hd.get('h1_count',0)}, "
                f"H2×{hd.get('h2_count',0)}, H3×{hd.get('h3_count',0)}"
            )
            if hd.get("hierarchy_issues"):
                heading_ctx += f" — {', '.join(hd['hierarchy_issues'][:2])}"

        faq_ctx = ""
        if "schema_markup" in breakdown:
            sm = breakdown["schema_markup"]
            if sm.get("faq_recommendation") == "missing":
                faq_ctx = "  FAQ content detected but FAQPage schema is missing.\n"
            elif sm.get("faq_recommendation") == "present":
                faq_ctx = "  FAQPage schema correctly implemented.\n"

        prompt = (
            f"{site_ctx_block}"
            f"POST ANALYSIS:\n"
            f"  Title       : {title}\n"
            f"  Health score: {score}/100\n"
            f"  Word count  : {word_count:,} words\n"
            f"  Issues      : {'; '.join(issues) if issues else 'None'}\n"
            + (f"{heading_ctx}\n" if heading_ctx else "")
            + faq_ctx
            + "\nProvide exactly 3 specific, actionable improvements for this post. "
            + (
                f"Write them in a {site_context.get('brand_tone', 'professional')} tone, "
                f"relevant to a {site_context.get('industry', 'WordPress')} site "
                f"targeting {site_context.get('target_audience', 'their audience')}. "
                if site_context else ""
            )
            + "Reference the actual issues above — do not give generic advice. "
            'Respond with JSON only: {"recommendations": ["...", "...", "..."]}'
        )
        # FAST_MODEL: this is a bounded, well-structured task (3 short fixes
        # for explicitly-listed issues) run per-post across potentially
        # thousands of posts — the flagship model's extra reasoning power
        # isn't needed here, and the cheaper tier cuts the dominant AI cost
        # in the whole system by ~10x.
        result = await ai_engine.generate_json(prompt, max_tokens=700, model=FAST_MODEL)
        recs = result.get("recommendations", [])
        return "\n".join(recs[:3]) if recs else ""
    except Exception:
        return None


def _prioritize_ai_candidates(candidates: list[dict], budget: int) -> tuple[list[dict], list[dict]]:
    """Split AI-recommendation candidates into (to_process, deferred) at the
    per-run budget cutoff.

    Worst health score first, ties broken by higher traffic — an enterprise
    site can surface thousands of candidates on a first crawl; a hard per-run
    cap keeps OpenAI cost and run time bounded and predictable while always
    spending it on the content that needs the most help and reaches the most
    readers. Deferred candidates keep their stale ai_rec_hash untouched, so
    they're simply reconsidered (and very likely prioritized again) next run.
    """
    ordered = sorted(candidates, key=lambda c: (c["score"], -c["traffic"]))
    cutoff = max(0, budget)
    return ordered[:cutoff], ordered[cutoff:]


def _interleave(a: list[dict], b: list[dict]) -> list[dict]:
    """Round-robin merge, not concatenation — keeps the smaller collection
    (typically pages, vastly outnumbered by posts on most sites) spread
    evenly through the combined list instead of clustered at the very end,
    where a per-run batch cap would otherwise starve it indefinitely."""
    merged: list[dict] = []
    for i in range(max(len(a), len(b))):
        if i < len(a):
            merged.append(a[i])
        if i < len(b):
            merged.append(b[i])
    return merged


def _analysis_priority_key(last_analyzed_at: datetime | None) -> tuple:
    """Sort key for deciding which changed items get analyzed THIS run when
    there are more than CONTENT_ANALYSIS_BATCH_SIZE of them: never-analyzed
    (None) always sorts first, then oldest-analyzed first. Without this, a
    site with far more posts than pages (the common case) would sort
    however WordPress happened to return them, and pages could go
    unanalyzed run after run — exactly the bug this fixes."""
    if last_analyzed_at is None:
        return (0, datetime.min.replace(tzinfo=timezone.utc))
    return (1, last_analyzed_at)


class ContentScorer(BaseAgent):
    async def run(self, site_id: str) -> list[Alert]:
        result = await self.db.execute(select(Site).where(Site.id == site_id))
        site = result.scalar_one_or_none()
        if not site:
            return []

        # A fetch failure is allowed to raise here (previously it was
        # swallowed and the run returned an empty alert list — identical to
        # a genuinely successful "nothing to do" run, with no visible sign
        # anywhere that the scan hadn't actually happened).
        wp_posts = await get_posts_cached(site.url, site.api_key)
        wp_pages = await get_pages_cached(site.url, site.api_key)
        wp_items = _interleave(wp_posts, wp_pages)

        alerts: list[Alert] = []

        # Load all ContentPost rows for this site once — one query, not one per post
        posts_r = await self.db.execute(
            select(ContentPost).where(ContentPost.site_id == site_id)
        )
        db_posts_by_wp_id = {p.wp_post_id: p for p in posts_r.scalars().all()}

        # Existing content-health alerts, keyed by the post they're about —
        # looked up in Python from one query rather than a JSON-path SQL
        # filter. Only alerts for posts actually re-analyzed THIS run get
        # replaced (see the batch cap below); every other post's existing
        # alert is left alone rather than being deleted up front and
        # possibly never recreated for several runs.
        existing_alerts_r = await self.db.execute(
            select(Alert).where(
                Alert.site_id == site_id, Alert.agent == "optimizer", Alert.type == "content_health",
            )
        )
        alerts_by_post_id = {
            a.metadata_.get("post_id"): a for a in existing_alerts_r.scalars().all() if a.metadata_
        }

        def _wp_modified(item: dict) -> datetime | None:
            return parse_wp_datetime(item.get("modified_gmt") or item.get("modified"))

        def _unchanged(item: dict) -> bool:
            """True when WordPress reports no edits since we last fully
            analyzed this post/page — skips the live-page crawl for it,
            which is the most expensive per-post step after the AI call."""
            wp_id = item.get("id")
            existing = db_posts_by_wp_id.get(wp_id) if wp_id else None
            if not existing or existing.wp_modified_at is None:
                return False
            item_modified = _wp_modified(item)
            return item_modified is not None and item_modified == existing.wp_modified_at

        def _title_of(item: dict) -> str:
            raw = item.get("title", {})
            title = raw.get("rendered", "") if isinstance(raw, dict) else str(raw)
            return re.sub(r"<[^>]+>", "", title).strip()

        def _content_of(item: dict) -> str:
            raw = item.get("content", {})
            return raw.get("rendered", "") if isinstance(raw, dict) else ""

        def _priority_key(item: dict) -> tuple:
            post = db_posts_by_wp_id.get(item.get("id"))
            return _analysis_priority_key(post.last_analyzed_at if post else None)

        # ── Split into "gets full analysis this run" vs. "deferred" — an
        # enterprise site can have thousands of never-analyzed items, and
        # trying all of them in one run is exactly what used to blow the
        # job timeout and lose every bit of progress. Unchanged items cost
        # nothing (no live fetch, no re-analysis) so they're never deferred.
        changed_items: list[dict] = []
        unchanged_items: list[dict] = []
        for item in wp_items:
            if not item.get("link"):
                continue
            (unchanged_items if _unchanged(item) else changed_items).append(item)

        changed_items.sort(key=_priority_key)
        batch_size = settings.CONTENT_ANALYSIS_BATCH_SIZE
        to_analyze = changed_items[:batch_size]
        deferred_count = len(changed_items) - len(to_analyze)

        if unchanged_items:
            logger.info(
                "ContentScorer %s: reusing prior analysis for %d/%d unchanged post(s)/page(s)",
                site.url, len(unchanged_items), len(wp_items),
            )
        if deferred_count:
            logger.info(
                "ContentScorer %s: analyzing %d/%d changed item(s) this run — %d deferred to next run",
                site.url, len(to_analyze), len(changed_items), deferred_count,
            )

        ai_candidates: list[dict] = []

        # Even though these posts haven't changed, a previous run may have
        # deferred their AI recommendation for budget reasons — catch those
        # up here from already-stored analysis, with no live fetch or
        # re-analysis needed.
        for wp_item in unchanged_items:
            post = db_posts_by_wp_id.get(wp_item.get("id"))
            if not post:
                continue
            title_str = _title_of(wp_item)
            content_text = _content_of(wp_item)
            ai_input_hash = hashlib.sha256(
                f"{title_str}|{post.health_score}|{content_text}".encode()
            ).hexdigest()
            if post.ai_rec_hash != ai_input_hash:
                ai_candidates.append({
                    "post": post, "title": title_str, "score": post.health_score,
                    "word_count": post.word_count or 0, "issues": post.issues or [],
                    "breakdown": post.score_breakdown or {},
                    "hash": ai_input_hash, "url": wp_item["link"], "traffic": post.traffic_30d or 0,
                })

        # ── Pass 1: which items in this batch need a live-page crawl ────────
        local_schema: dict[str, list[str]] = {}
        needs_live_fetch: list[str] = []
        for wp_item in to_analyze:
            url = wp_item["link"]
            found = _local_schema_types(wp_item)
            local_schema[url] = found
            if _needs_live_fetch(found, _content_of(wp_item)):
                needs_live_fetch.append(url)

        # Live-page crawl only for posts _needs_live_fetch() flags as needing
        # confirmation — see its docstring. One fetch yields all signals.
        # Max 8 concurrent, 6s timeout each.
        _sem = asyncio.Semaphore(8)

        async def _bounded_fetch(url: str) -> dict:
            async with _sem:
                try:
                    return await asyncio.wait_for(_fetch_page_signals(url), timeout=6)
                except asyncio.TimeoutError:
                    return _empty_signals()

        live_results = await asyncio.gather(*[_bounded_fetch(u) for u in needs_live_fetch])
        live_by_url: dict[str, dict] = dict(zip(needs_live_fetch, live_results))

        # ── Pass 2: rule-based scoring for this batch, committing every
        # CONTENT_COMMIT_EVERY items — a mid-run timeout or crash then only
        # costs the current partial chunk, never the whole run's progress.
        commit_every = settings.CONTENT_COMMIT_EVERY
        processed_since_commit = 0

        for wp_item in to_analyze:
            wp_id = wp_item.get("id")
            post = db_posts_by_wp_id.get(wp_id) if wp_id else None
            if not post:
                continue
            post_url = wp_item["link"]

            try:
                signals = live_by_url.get(post_url)
                # A live fetch scans the ENTIRE rendered page (head + body +
                # footer + plugin output), so it's a superset of the local
                # body/Yoast scan — prefer it whenever we have it. Using
                # `local or live` here previously meant that whenever local
                # scan found ANY schema (even just Article/WebPage from Yoast),
                # the live fetch's result was discarded outright — silently
                # dropping FAQPage schema injected elsewhere on the page and
                # false-flagging it as missing.
                page_schema_types = signals["schema_types"] if signals is not None else (local_schema.get(post_url) or [])
                live_h1 = signals["h1_count"] if signals else None

                health_score, issues, breakdown, word_count, reading_time = _analyze(
                    wp_item, site.url,
                    extra_schema_types=page_schema_types,
                    live_h1_count=live_h1,
                )

                post.health_score = health_score
                post.issues = issues
                post.word_count = word_count
                post.reading_time_minutes = reading_time
                post.score_breakdown = breakdown
                post.last_analyzed_at = datetime.now(timezone.utc)
                post.wp_modified_at = _wp_modified(wp_item)

                # Replace this post's existing alert (if any) — every other
                # post's alert (not reanalyzed this run) is left untouched.
                stale_alert = alerts_by_post_id.pop(post.id, None)
                if stale_alert:
                    await self.db.delete(stale_alert)

                if health_score < 40:
                    severity = "critical" if health_score < 20 else "warning"
                    alerts.append(await self.create_alert(
                        site_id=site_id,
                        agent="optimizer",
                        severity=severity,
                        type_="content_health",
                        title=f"Poor content health: {post.title[:60]}",
                        description=f"Score {health_score}/100. {issues[0] if issues else ''}",
                        metadata={
                            "post_id": post.id,
                            "wp_post_id": wp_id,
                            "health_score": health_score,
                            "issues": issues,
                            "url": post.url,
                        },
                    ))

                title_str = _title_of(wp_item)
                content_text = _content_of(wp_item)
                ai_input_hash = hashlib.sha256(
                    f"{title_str}|{health_score}|{content_text}".encode()
                ).hexdigest()

                if post.ai_rec_hash != ai_input_hash:
                    ai_candidates.append({
                        "post": post, "title": title_str, "score": health_score,
                        "word_count": word_count, "issues": issues, "breakdown": breakdown,
                        "hash": ai_input_hash, "url": post_url, "traffic": post.traffic_30d or 0,
                    })
            except Exception as exc:
                # One malformed post must never abort the whole batch — that
                # was the difference between "1,800 pages/posts scored" and
                # "zero, because item #4 threw".
                logger.warning("ContentScorer: analysis failed for %s (wp_id=%s): %s", post_url, wp_id, exc)
                continue

            processed_since_commit += 1
            if processed_since_commit >= commit_every:
                await self.db.commit()
                processed_since_commit = 0

        if processed_since_commit:
            await self.db.commit()

        # ── Pass 3: spend the AI budget on the posts that need it most,
        # in small concurrent chunks — committing after each chunk for the
        # same reason as Pass 2. AI calls are the slowest, least predictable
        # step, so this is where a timeout is most likely to land.
        budget = settings.CONTENT_AI_BUDGET_PER_RUN
        to_process, ai_deferred = _prioritize_ai_candidates(ai_candidates, budget)

        ai_sem = asyncio.Semaphore(settings.CONTENT_AI_CONCURRENCY)

        async def _generate(candidate: dict) -> None:
            async with ai_sem:
                try:
                    ai_rec = await asyncio.wait_for(
                        _generate_ai_recommendation(
                            candidate["title"], candidate["score"], candidate["word_count"],
                            candidate["issues"], candidate["breakdown"],
                            site_context=site.site_context or None,
                        ),
                        timeout=30,
                    )
                except Exception as exc:
                    logger.warning("AI recommendation failed for %s: %s", candidate["url"], exc)
                    ai_rec = None
                # None = generation failed — leave any existing text alone.
                # "" = genuinely clean — clear the stale recommendation.
                if ai_rec is not None:
                    candidate["post"].ai_recommendation = ai_rec or None
                    candidate["post"].ai_rec_hash = candidate["hash"]

        chunk_size = max(commit_every, settings.CONTENT_AI_CONCURRENCY)
        for i in range(0, len(to_process), chunk_size):
            chunk = to_process[i:i + chunk_size]
            await asyncio.gather(*[_generate(c) for c in chunk])
            await self.db.commit()

        if ai_deferred:
            logger.info(
                "ContentScorer %s: AI budget (%d) reached — deferred %d/%d recommendation(s) to the next run",
                site.url, budget, len(ai_deferred), len(ai_candidates),
            )

        # Update site's overall health score from post averages
        all_posts_result = await self.db.execute(
            select(ContentPost).where(ContentPost.site_id == site_id)
        )
        all_posts = all_posts_result.scalars().all()
        if all_posts:
            site.health_score = sum(p.health_score for p in all_posts) // len(all_posts)

        # Populate traffic_30d from GA if connected
        try:
            from app.api.auth import get_google_token
            from app.connectors.analytics import AnalyticsConnector
            from app.database.models import SiteConfig
            from urllib.parse import urlparse as _urlparse

            token = await get_google_token(self.db)
            if token:
                cfg_r = await self.db.execute(
                    select(SiteConfig).where(SiteConfig.site_id == site_id)
                )
                cfg = cfg_r.scalar_one_or_none()
                if cfg and cfg.ga_property_id:
                    ga = AnalyticsConnector(token.access_token)
                    top_pages = await ga.get_top_pages(cfg.ga_property_id, days=30, limit=200)
                    path_views = {p["path"]: p["views"] for p in top_pages}
                    for post in all_posts:
                        path = _urlparse(post.url).path
                        if path in path_views:
                            post.traffic_30d = path_views[path]
        except Exception as exc:
            logger.warning("GA traffic population failed for site %s: %s", site_id, exc)

        await self.db.commit()
        return alerts
