"""Re-checking a single broken link, and suggesting what it should have been.

Two jobs, both about the gap between "we found a problem" and "the problem is
fixed".

Re-check exists because the sweep rotates: after editing a page you would
otherwise wait for the link's turn to come round again, with no way to confirm
the fix. It verifies one URL now.

Suggestions exist for a specific, common defect. An `href` containing prose —
someone pasting a sentence into WordPress's link field — carries the text that
was meant to be the *anchor*, not the destination. That text is usually enough
to identify the page the author intended:

    https://price%20quote                        ->  "price quote"
    http://choosing%20the%20right%20saas...       ->  "choosing the right saas..."

So the prose is decoded and matched against the site's own pages, ranked by
the same IDF-weighted overlap the internal linker uses to decide what is
related. One definition of relevance, not two.

Nothing here edits anything. A suggestion is offered with the terms it matched
on, so the person applying it can see why it was suggested and disagree.
"""
import html as html_lib
import logging
import math
import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urlparse

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.agents.optimizer.internal_linker import _idf_weights, _keywords
from app.agents.watchdog.link_checker import (
    _BROWSER_HEADERS,
    _check_url,
    _classify,
    is_malformed_host,
)
from app.database.models import Alert, ContentPost

logger = logging.getLogger(__name__)

# A suggestion must clear this weighted-overlap score to be offered at all.
# Matching the internal linker's threshold deliberately: below it, the overlap
# is common words, and three bad guesses are worse than saying "no idea".
MIN_SUGGESTION_SCORE = 3.0
MAX_SUGGESTIONS = 3

# A punycode label, as produced when something tries to coerce a sentence into
# a hostname: `xn--<content>-<checksum>`. Matched on the still-encoded URL,
# where the label is contiguous — after decoding, its %20s become spaces and
# the checksum is left stranded as a word. The content is kept, because it is
# the prose we are after; only the marker and the checksum go.
_PUNYCODE_LABEL = re.compile(r"xn--(.*?)-[a-z0-9]{2,8}(?=[./]|$)", re.I)
_PUNYCODE_MARKER = re.compile(r"xn--", re.I)


def _stem(word: str) -> str:
    """Collapse a simple plural, so "quote" matches "Quotes".

    Deliberately crude and deliberately local. The failure it fixes was
    concrete: for "price quote" the obviously right page — "Tailored Price
    Quotes" — lost to an article about Slack block quotes, on a trailing "s".

    Not applied to the shared `_keywords`, because the internal linker's
    threshold is calibrated against that scoring and its own suggestions are
    surfaced elsewhere; changing it here would quietly change those too.
    """
    # "es" is only the plural marker after a sibilant — boxes, churches,
    # dishes. Elsewhere the "e" belongs to the word, and stripping both
    # letters turns "quotes" into "quot" while "quote" stays whole, which is
    # exactly the mismatch this function exists to remove.
    if len(word) > 4 and word.endswith(("ses", "xes", "zes", "ches", "shes")):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        return word[:-1]
    return word


def _stems(words: set[str]) -> dict[str, str]:
    """Stem -> the surface form to show for it."""
    out: dict[str, str] = {}
    for w in sorted(words):
        out.setdefault(_stem(w), w)
    return out


def _clean_title(title: str) -> str:
    """WordPress stores titles with entities intact — `Tailored &#8211; Price`.
    Fine for matching, wrong to show: the suggestion panel puts these in front
    of someone choosing between them."""
    return html_lib.unescape(re.sub(r"<[^>]+>", "", title or "")).strip()


@dataclass(frozen=True)
class Suggestion:
    url: str
    title: str
    score: float
    # The words this was matched on. Shown so a suggestion can be judged
    # rather than trusted.
    matched: list[str]


def prose_from_url(url: str) -> str:
    """The human text buried in a malformed href.

    Everything after the scheme, percent-decoded, with the punycode wreckage
    and separator characters removed. What comes back is the sentence somebody
    pasted, which is the only clue to what they meant.
    """
    parsed = urlparse(url)
    raw = f"{parsed.netloc}{parsed.path}"
    raw = _PUNYCODE_LABEL.sub(r"\1", raw)
    raw = _PUNYCODE_MARKER.sub(" ", raw)
    text = unquote(raw)
    text = re.sub(r"[/._\-+]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


async def suggest_replacements(
    db: AsyncSession, site_id: str, broken_url: str, limit: int = MAX_SUGGESTIONS
) -> list[Suggestion]:
    """Pages on this site that the prose in a malformed href probably meant.

    Only for malformed hrefs. A link to a real page that happens to 404 is a
    different problem — the destination is known, and guessing a replacement
    for it would be inventing intent nobody expressed.
    """
    if not is_malformed_host(broken_url):
        return []

    wanted = _stems(_keywords(prose_from_url(broken_url)))
    if not wanted:
        return []

    rows = (await db.execute(
        select(ContentPost.title, ContentPost.url)
        .where(ContentPost.site_id == site_id, ContentPost.url.isnot(None))
    )).all()
    if not rows:
        return []

    # IDF over this site's own titles, so a word every page shares counts for
    # little and a distinctive one carries the match.
    keywords_by_page = {
        url: set(_stems(_keywords(title or "")))
        for title, url in rows if url
    }
    idf = _idf_weights(keywords_by_page)

    scored: list[Suggestion] = []
    for title, url in rows:
        if not url:
            continue
        shared = set(wanted) & keywords_by_page.get(url, set())
        if not shared:
            continue
        # Weighted by breadth as well as rarity. A raw IDF sum lets one
        # coincidentally rare word outrank a page matching three of the
        # prose's terms — which is how "Slack Block Quote Migration" beat the
        # pricing page for "price quote". sqrt, not a plain multiply, so
        # breadth improves a match without swamping the rarity signal.
        score = sum(idf.get(w, 1.0) for w in shared) * math.sqrt(len(shared))
        if score < MIN_SUGGESTION_SCORE:
            continue
        scored.append(Suggestion(
            url=url,
            title=_clean_title(title) or url,
            score=round(score, 2),
            # Reported in the prose's own words, not the stems.
            matched=[wanted[k] for k in
                     sorted(shared, key=lambda w: idf.get(w, 1.0), reverse=True)[:5]],
        ))

    scored.sort(key=lambda s: (-s.score, s.url))
    return scored[:limit]


async def recheck_link(
    db: AsyncSession, site_id: str, site_url: str, url: str
) -> dict[str, Any]:
    """Verify one link now and reconcile its alert.

    Same verdict logic as the sweep — a hand-triggered check and a scheduled
    one must not disagree about whether a link is broken.
    """
    from app.agents.watchdog.link_checker import LinkChecker

    if is_malformed_host(url):
        # No request to make: this host cannot exist. Re-checking it over the
        # network would spend three retries confirming the obvious.
        status, severity = 0, "critical"
    else:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(10.0, connect=5.0), headers=_BROWSER_HEADERS
        ) as client:
            status = await _check_url(client, url)
        # Internal links are judged more strictly — a 401/403 on your own
        # domain is a page your readers cannot open, not bot protection.
        is_internal = urlparse(url).netloc == urlparse(site_url).netloc
        severity = _classify(status, is_internal)

    rows = (await db.execute(
        select(Alert).where(
            Alert.site_id == site_id,
            Alert.agent == "watchdog",
            Alert.type == "broken_link",
        )
    )).scalars().all()
    matching = [a for a in rows if (a.metadata_ or {}).get("url") == url]

    fixed = False
    if severity is None:
        # Verified working — the finding is resolved, not merely quiet.
        for alert in matching:
            await db.delete(alert)
        fixed = bool(matching)
    elif matching:
        agent = LinkChecker(db)
        keep = min(matching, key=lambda a: a.created_at)
        await agent.update_alert(
            keep, severity=severity, title=keep.title,
            description=keep.description, metadata={**(keep.metadata_ or {}), "status_code": status},
        )

    await db.flush()
    return {
        "url": url,
        "status_code": status,
        "severity": severity,
        "fixed": fixed,
        "malformed": is_malformed_host(url),
    }


__all__ = [
    "MAX_SUGGESTIONS",
    "MIN_SUGGESTION_SCORE",
    "Suggestion",
    "prose_from_url",
    "recheck_link",
    "suggest_replacements",
]
