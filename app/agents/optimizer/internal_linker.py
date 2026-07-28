"""Internal Linker — suggests internal links between semantically related posts.

Candidates are found via an inverted keyword index (keyword -> post ids)
rather than comparing every post against every other post: comparing all
pairs directly is O(n^2), which is fine for a few hundred posts but
infeasible for an enterprise site with thousands. The index means a post
only gets compared against posts it already shares at least one keyword
with, so cost scales with actual keyword overlap instead of site size.

Shared keywords are weighted by rarity (IDF) rather than counted. A site
whose whole blog is about "cloud migration" will have "migration" in most
titles — two posts sharing only that word aren't meaningfully related to
each other, just both on-topic for the site. Weighting by how few OTHER
posts also use a word means a rare, specific shared term (e.g. "sharepoint")
correctly outweighs a handful of generic ones. This part decides WHICH
pairs are worth suggesting — it only ever looks at titles, and is cheap.

Anchor text is a stricter, separate concern, and title-only data isn't
enough to get it right: an early version picked anchor text from the
TARGET page's top Search Console query with no check that the phrase had
anything to do with the SOURCE page at all. That produced a real, broken
suggestion — inserting the anchor "can we send mail from gmail to outlook"
into an article titled "Export Outlook Emails to Gmail" (the opposite
direction), a phrase that never appeared anywhere in that article's actual
text. So every suggestion here now fetches the proposed SOURCE post's real
WordPress body content and requires the anchor phrase to be verifiably
present in it — preferring a real GSC query for the target IF it actually
appears in the source's own words, else a phrase extracted directly from
the source's own text around a genuinely shared topic word. A pair is
suggested only if a grounded anchor exists in at least one direction;
otherwise it's dropped rather than guessed at.
"""
import asyncio
import html
import math
import re
from typing import Any

from sqlalchemy import delete, select

from app.agents.base import BaseAgent
from app.connectors.wordpress import WordPressConnector
from app.database.models import Alert, ContentPost, Site

_STOP_WORDS = {
    "the", "and", "for", "how", "with", "your", "from", "this", "that",
    "what", "when", "where", "which", "have", "will", "been", "they",
    "their", "about", "into", "more", "also", "some", "than",
}

# A pair must clear this weighted-overlap score to be suggested. Calibrated
# against the smoothed IDF formula below: a word every post shares scores
# ~1.0, so two such words alone (~2.0) fall short — the pair needs either
# one moderately distinctive shared term or several weakly common ones.
_MIN_PAIR_SCORE = 3.0

# Ceiling on candidates put through the (expensive: live WP + GSC lookups)
# verification pass — bounds Alert volume and keeps a run's network cost
# proportional, not O(every candidate pair on an enterprise site). Some of
# these will still be dropped for lacking a verifiable anchor in either
# direction, so the final suggestion count is typically somewhat lower.
_MAX_CANDIDATES_TO_VERIFY = 40

# A real GSC query is only used as anchor text if it has some actual search
# visibility (not a one-off fluke), reads as a phrase (not a single word or
# a whole sentence), AND is confirmed present in the chosen source's actual text.
_MIN_QUERY_IMPRESSIONS = 5
_MIN_QUERY_WORDS = 2
_MAX_QUERY_WORDS = 8

# Words of context either side of a matched keyword when building a
# fallback anchor straight from the source's own text.
_EXTRACTED_ANCHOR_WINDOW = 5

# Per-page WordPress/GSC lookups are real network calls — bounded
# concurrency keeps a verification pass from serializing into dozens of
# sequential round-trips.
_FETCH_CONCURRENCY = 5


def _keywords(title: str) -> set[str]:
    words = re.findall(r"\b[a-z]{4,}\b", title.lower())
    return {w for w in words if w not in _STOP_WORDS}


def _idf_weights(keywords_by_post: dict[str, set[str]]) -> dict[str, float]:
    """Smoothed IDF per keyword: log((1+N)/(1+df)) + 1 — always >= 1, and
    lower for words that show up in more of the site's posts."""
    n = len(keywords_by_post)
    doc_freq: dict[str, int] = {}
    for kw in keywords_by_post.values():
        for word in kw:
            doc_freq[word] = doc_freq.get(word, 0) + 1
    return {word: math.log((1 + n) / (1 + df)) + 1 for word, df in doc_freq.items()}


def _pair_score(shared: set[str], idf: dict[str, float]) -> float:
    return sum(idf.get(w, 1.0) for w in shared)


def _choose_direction(
    post_a: tuple[str, int, int], post_b: tuple[str, int, int],
) -> tuple[tuple[str, int, int], tuple[str, int, int]]:
    """Pick the PREFERRED link direction: the higher-traffic post has more
    link equity to pass on, so it becomes the source; the weaker post (the
    one that benefits most from an inbound link) becomes the target. Ties
    fall back to health_score, then id, so the choice is deterministic.
    This is only a first attempt — verification may still flip it (see
    `_resolve_link`) if no grounded anchor exists in this direction.
    """
    a_id, a_traffic, a_health = post_a
    b_id, b_traffic, b_health = post_b
    if a_traffic != b_traffic:
        return (post_a, post_b) if a_traffic > b_traffic else (post_b, post_a)
    if a_health != b_health:
        return (post_a, post_b) if a_health > b_health else (post_b, post_a)
    return (post_a, post_b) if a_id < b_id else (post_b, post_a)


def _extract_text(raw_html: str) -> str:
    """Plain, entity-decoded text from a WP `content.rendered` HTML body —
    without decoding entities, literal text like "&#038;" would never
    match a "clean" candidate phrase even when the words are really there."""
    text = re.sub(r"<[^>]+>", " ", raw_html or "")
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _phrase_in_text(phrase: str, text: str) -> bool:
    return bool(phrase) and bool(text) and phrase.strip().lower() in text.lower()


def _pick_query_anchor(queries: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Best real search query to use as anchor text for a page: a natural
    multi-word phrase with real, non-trivial search visibility — ranked by
    clicks (people who actually chose this page for that query), then
    impressions. Presence in the eventual source text is checked separately
    by the caller; this only filters to phrases worth checking at all."""
    candidates = [
        q for q in queries
        if q.get("impressions", 0) >= _MIN_QUERY_IMPRESSIONS
        and _MIN_QUERY_WORDS <= len(q["query"].split()) <= _MAX_QUERY_WORDS
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda q: (q.get("clicks", 0), q.get("impressions", 0)))


_WEAK_EDGE_WORDS = {
    "a", "an", "the", "to", "for", "of", "in", "on", "at", "and", "or", "with", "from", "by", "as", "is",
}


def _sentences(text: str) -> list[str]:
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+", text) if s.strip()]


def _trim_weak_edges(phrase: str) -> str:
    """Drop leading/trailing prepositions, articles, and conjunctions — a
    window cut mid-sentence otherwise regularly ends on a dangling "to" or
    "for", which reads as an obvious auto-generated fragment rather than a
    real, editable link phrase."""
    words = phrase.split()
    while len(words) > 2 and words[0].lower().strip(".,;:!?\"'()") in _WEAK_EDGE_WORDS:
        words.pop(0)
    while len(words) > 2 and words[-1].lower().strip(".,;:!?\"'()") in _WEAK_EDGE_WORDS:
        words.pop()
    return " ".join(words)


def _extract_anchor_from_text(
    source_text: str, shared_keywords_ranked: list[str], window: int = _EXTRACTED_ANCHOR_WINDOW,
) -> str | None:
    """A short phrase, in the source's own real wording, built around the
    most distinctive shared keyword the source text actually contains —
    always a genuine, literal, immediately-findable substring of that post,
    since it's extracted directly from it rather than proposed from title
    keywords alone. Bounded to a single sentence (never spans two unrelated
    sentences into one nonsensical phrase) and trimmed of dangling
    prepositions/articles at either edge.
    """
    if not source_text:
        return None
    for kw in shared_keywords_ranked:
        for sentence in _sentences(source_text):
            words = sentence.split()
            lower_words = [w.lower() for w in words]
            for i, w in enumerate(lower_words):
                if kw in w:
                    start = max(0, i - window // 2)
                    end = min(len(words), i + window // 2 + 1)
                    phrase = " ".join(words[start:end]).strip(" .,;:!?\"'()")
                    phrase = _trim_weak_edges(phrase)
                    if phrase and len(phrase.split()) >= 2:
                        return phrase
    return None


def _resolve_link(
    post_x: ContentPost,
    post_y: ContentPost,
    shared_keywords_ranked: list[str],
    source_texts: dict[str, str],
    queries_by_target_url: dict[str, list[dict[str, Any]]],
) -> tuple[ContentPost, ContentPost, str, str, dict[str, Any] | None] | None:
    """Try linking x -> y, then y -> x; for each direction, prefer a real
    search query for the target IF it's actually present in that source's
    text, else a phrase extracted straight from the source's own text.
    Returns None if neither direction has a source with any verifiable
    anchor at all — better to suggest nothing than something unusable.
    """
    for source, target in ((post_x, post_y), (post_y, post_x)):
        source_text = source_texts.get(source.id, "")
        if not source_text:
            continue

        query_match = _pick_query_anchor(queries_by_target_url.get(target.url, []))
        if query_match and _phrase_in_text(query_match["query"], source_text):
            return source, target, query_match["query"], "search_query", query_match

        extracted = _extract_anchor_from_text(source_text, shared_keywords_ranked)
        if extracted:
            return source, target, extracted, "source_text", None

    return None


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

        idf = _idf_weights(keywords_by_post)
        posts_by_id = {p.id: p for p in posts}
        seen_pairs: set[frozenset[str]] = set()

        # Score every candidate pair first, so the verification cap below
        # spends its budget on the strongest matches site-wide.
        candidates: list[tuple[float, str, str, set[str]]] = []
        for post in posts:
            kw = keywords_by_post[post.id]
            if not kw:
                continue

            candidate_ids = {pid for word in kw for pid in posts_by_keyword[word]} - {post.id}
            for other_id in candidate_ids:
                pair = frozenset([post.id, other_id])
                if pair in seen_pairs:
                    continue
                seen_pairs.add(pair)

                shared = kw & keywords_by_post[other_id]
                score = _pair_score(shared, idf)
                if score >= _MIN_PAIR_SCORE:
                    candidates.append((score, post.id, other_id, shared))

        candidates.sort(key=lambda c: c[0], reverse=True)
        top = candidates[:_MAX_CANDIDATES_TO_VERIFY]

        # Every post that could end up on either side of a link needs its
        # real content fetched (to verify anchors) — deduped, since the same
        # post commonly appears in several candidate pairs.
        involved_posts = list({posts_by_id[pid].id: posts_by_id[pid] for score, a, b, _ in top for pid in (a, b)}.values())

        source_texts, queries_by_target_url = await asyncio.gather(
            self._fetch_source_texts(site, involved_posts),
            self._fetch_gsc_queries(site, site_id, involved_posts),
        )

        alerts: list[Alert] = []
        for score, id_a, id_b, shared in top:
            post_a, post_b = posts_by_id[id_a], posts_by_id[id_b]
            shared_ranked = sorted(shared, key=lambda w: idf.get(w, 1.0), reverse=True)

            preferred = _choose_direction(
                (post_a.id, post_a.traffic_30d, post_a.health_score),
                (post_b.id, post_b.traffic_30d, post_b.health_score),
            )
            preferred_source, preferred_target = posts_by_id[preferred[0][0]], posts_by_id[preferred[1][0]]

            resolution = _resolve_link(
                preferred_source, preferred_target, shared_ranked, source_texts, queries_by_target_url,
            )
            if resolution is None:
                continue

            source_post, target_post, anchor, anchor_source, query_match = resolution

            alert = await self.create_alert(
                site_id=site_id,
                agent="optimizer",
                severity="info",
                type_="internal_link",
                title=f"Link opportunity: {target_post.title[:50]}",
                description=(
                    f'Add a link in "{source_post.title}" pointing to "{target_post.title}" '
                    f'using anchor text "{anchor}".'
                ),
                metadata={
                    "source_title": source_post.title,
                    "source_url": source_post.url,
                    "target_title": target_post.title,
                    "target_url": target_post.url,
                    "anchor_text": anchor,
                    "anchor_source": anchor_source,
                    "anchor_impressions": query_match["impressions"] if query_match else None,
                    "anchor_clicks": query_match["clicks"] if query_match else None,
                    "shared_keywords": shared_ranked,
                    "relevance_score": round(score, 2),
                },
            )
            alerts.append(alert)

        return alerts

    async def _fetch_source_texts(
        self, site: Site, posts: list[ContentPost],
    ) -> dict[str, str]:
        """Real, entity-decoded plain text of each post's actual WordPress
        body — fetched live so an anchor phrase can be REQUIRED to appear in
        it, not merely assumed from title keywords. Best-effort: any post
        that fails to fetch (or has no wp_post_id) simply has no verifiable
        anchor, and pairs depending on it fall back to the other direction
        or get dropped — never a fabricated anchor."""
        if not posts:
            return {}

        wp = WordPressConnector(site.url, site.api_key)
        semaphore = asyncio.Semaphore(_FETCH_CONCURRENCY)

        async def fetch_one(post: ContentPost) -> tuple[str, str]:
            if not post.wp_post_id:
                return post.id, ""
            async with semaphore:
                try:
                    wp_post = await wp.get_post(post.wp_post_id, content_type=post.content_type)
                    if not wp_post:
                        return post.id, ""
                    raw_html = (wp_post.get("content") or {}).get("rendered", "")
                    return post.id, _extract_text(raw_html)
                except Exception:
                    return post.id, ""

        try:
            results = await asyncio.gather(*[fetch_one(p) for p in posts])
        finally:
            await wp.close()
        return dict(results)

    async def _fetch_gsc_queries(
        self, site: Site, site_id: str, posts: list[ContentPost],
    ) -> dict[str, list[dict[str, Any]]]:
        """Real per-page search queries for each candidate page, if Google is
        connected — optional and best-effort, exactly like SEOAnalyzer's own
        GSC opportunity lookup: any failure just means every suggestion
        falls back to a source-text-extracted anchor, never a failed run."""
        try:
            from app.api.auth import get_google_token
            from app.connectors.search_console import SearchConsoleConnector
            from app.database.models import SiteConfig

            token = await get_google_token(self.db)
            if not token:
                return {}

            cfg_r = await self.db.execute(select(SiteConfig).where(SiteConfig.site_id == site_id))
            cfg = cfg_r.scalar_one_or_none()
            gsc_url = (cfg.gsc_site_url if cfg else None) or site.url
            gsc = SearchConsoleConnector(token.access_token)

            semaphore = asyncio.Semaphore(_FETCH_CONCURRENCY)

            async def fetch_one(post: ContentPost) -> tuple[str, list[dict[str, Any]]]:
                async with semaphore:
                    try:
                        return post.url, await gsc.get_page_queries(gsc_url, post.url)
                    except Exception:
                        return post.url, []

            results = await asyncio.gather(*[fetch_one(p) for p in posts])
            return dict(results)
        except Exception:
            return {}
