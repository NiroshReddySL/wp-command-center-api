"""Shared, short-TTL WordPress fetch cache for agent runs.

A full agent run (job executor or scheduler) has ContentScorer, SEOAnalyzer,
and LinkChecker each pull the same posts/pages from the same site within
minutes of each other. This module fetches once and shares the payload, so a
7-step run makes one posts request instead of three.

Fetches are uncapped full crawls (WordPressConnector.fetch_all — concurrent
pagination, no product-level item limit) so no post or page goes unseen on
a large site. In-process cache — correct for the current single-process
deployment. TTL is short (5 min) so scheduled runs hours apart always see
fresh content.
"""
import logging
import time

from app.connectors.wordpress import WordPressConnector

logger = logging.getLogger(__name__)

_TTL_SECONDS = 300

# key -> (fetched_at_monotonic, payload)
_cache: dict[str, tuple[float, list[dict]]] = {}


def invalidate(site_url: str) -> None:
    """Drop cached payloads for a site (call after a manual sync/flush)."""
    for key in [k for k in _cache if k.startswith(f"{site_url}::")]:
        _cache.pop(key, None)


async def _get(kind: str, site_url: str, api_key: str | None) -> list[dict]:
    key = f"{site_url}::{kind}"
    hit = _cache.get(key)
    if hit and time.monotonic() - hit[0] < _TTL_SECONDS:
        return hit[1]

    wp = WordPressConnector(site_url, api_key)
    try:
        if kind == "posts":
            payload = await wp.get_all_posts()
        else:
            payload = await wp.get_all_pages()
    finally:
        await wp.close()

    _cache[key] = (time.monotonic(), payload)
    return payload


async def get_posts_cached(site_url: str, api_key: str | None) -> list[dict]:
    return await _get("posts", site_url, api_key)


async def get_pages_cached(site_url: str, api_key: str | None) -> list[dict]:
    return await _get("pages", site_url, api_key)
