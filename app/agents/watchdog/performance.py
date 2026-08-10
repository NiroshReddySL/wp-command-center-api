"""Performance Monitor — uses Google PageSpeed Insights (desktop) for real Core Web Vitals.

Alert identity is the page URL: existing alerts update in place, so a
dismissed alert stays dismissed, created_at reflects when the problem was
first seen, and Teams notifications fire once per new problem instead of on
every 2-hour run.
"""
import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from sqlalchemy import delete, func, select

from app.agents.base import BaseAgent
from app.config import settings
from app.connectors.retry import request_with_retries
from app.database.models import Alert, ContentPost, PerformanceSnapshot, Site

SNAPSHOT_RETENTION_DAYS = 90

logger = logging.getLogger(__name__)

PSI_API = "https://www.googleapis.com/pagespeedonline/v5/runPagespeed"


async def _fetch_psi(
    client: httpx.AsyncClient, url: str, strategy: str = "desktop",
) -> dict | None:
    """
    Call PageSpeed Insights API. Returns parsed metrics dict or None on failure.
    PSI is free without a key (up to ~25 req/100s per IP).

    `strategy` is "desktop" (this monitor's site-wide sweep) or "mobile".
    Mobile is the one that matters for search: Google indexes and ranks on
    the mobile crawl, and mobile scores are routinely far worse than desktop
    for the same page — so per-post content analysis asks for mobile.
    """
    try:
        params = {"url": url, "strategy": strategy, "category": "performance"}
        if settings.PSI_API_KEY:
            params["key"] = settings.PSI_API_KEY  # 25k/day vs ~25/100s keyless
        resp = await request_with_retries(
            lambda: client.get(PSI_API, params=params, timeout=30.0),
            what=f"PSI {url}",
        )
        if resp.status_code != 200:
            hint = (
                " (quota — set PSI_API_KEY to raise the keyless ~25 req/100s limit)"
                if resp.status_code == 429 else ""
            )
            logger.warning(
                "PSI returned HTTP %d for %s%s — using TTFB fallback",
                resp.status_code, url, hint,
            )
            return None
        data = resp.json()
        cats = data.get("lighthouseResult", {}).get("categories", {})
        audits = data.get("lighthouseResult", {}).get("audits", {})
        metrics = audits.get("metrics", {}).get("details", {}).get("items", [{}])[0]

        score = int((cats.get("performance", {}).get("score") or 0) * 100)

        def _ms(key: str) -> float:
            return float(metrics.get(key) or 0)

        def _audit_ms(audit_key: str) -> float:
            val = audits.get(audit_key, {}).get("numericValue") or 0
            return float(val)

        lcp = _ms("largestContentfulPaint") or _audit_ms("largest-contentful-paint")
        cls = float(audits.get("cumulative-layout-shift", {}).get("numericValue") or 0)
        fid = _ms("totalBlockingTime") or _audit_ms("total-blocking-time")   # TBT as proxy
        ttfb = _audit_ms("server-response-time")

        return {
            "score": score,
            "lcp": lcp,
            "cls": cls,
            "fid": fid,
            "ttfb": ttfb,
        }
    except Exception as exc:
        # Silent here meant the score quietly became a TTFB estimate with no
        # way to find out why — PSI throttling looks identical to a parse bug.
        logger.warning("PSI lookup failed for %s (%s) — using TTFB fallback", url, exc)
        return None


async def _measure_ttfb_fallback(url: str) -> float:
    """Simple TTFB measurement as fallback when PSI is unavailable."""
    times: list[float] = []
    async with httpx.AsyncClient(
        timeout=15.0,
        follow_redirects=True,
        headers={"User-Agent": "Mozilla/5.0 (compatible; WP-Command-Center/1.0)", "Cache-Control": "no-cache"},
    ) as client:
        for _ in range(3):
            try:
                start = time.monotonic()
                await client.get(url)
                times.append((time.monotonic() - start) * 1000)
            except Exception:
                pass
            await asyncio.sleep(0.3)
    if not times:
        raise RuntimeError("All requests failed")
    times_sorted = sorted(times)
    trimmed = times_sorted[1:-1] if len(times_sorted) >= 3 else times_sorted
    return sum(trimmed) / len(trimmed)


def _score_from_ttfb(ttfb_ms: float) -> int:
    if ttfb_ms < 600:
        return 95
    if ttfb_ms < 800:
        return 85
    if ttfb_ms < 1200:
        return 72
    if ttfb_ms < 1800:
        return 58
    if ttfb_ms < 2500:
        return 38
    return 20


def _lcp_grade(lcp_ms: float) -> str:
    if lcp_ms <= 2500:
        return "Good"
    if lcp_ms <= 4000:
        return "Needs improvement"
    return "Poor"


def _cls_grade(cls: float) -> str:
    if cls <= 0.1:
        return "Good"
    if cls <= 0.25:
        return "Needs improvement"
    return "Poor"


def _score_grade(score: int) -> str:
    if score >= 90:
        return "Good"
    if score >= 50:
        return "Needs improvement"
    return "Poor"


@dataclass(frozen=True)
class Measurement:
    """What one page measured, or why there is no measurement.

    `source` says how the number was arrived at — a PSI score and a TTFB
    estimate are not the same claim, and collapsing them was what let a
    throttled run look like a measured one.
    """

    url: str
    score: int = 0
    lcp: float = 0.0
    cls: float = 0.0
    fid: float = 0.0
    ttfb: float = 0.0
    source: str = "psi"          # "psi" | "ttfb" | "error"
    error: str | None = None


@dataclass(frozen=True)
class Verdict:
    """What a measurement means. `severity is None` means healthy — clear any
    alert this page had rather than leaving a fixed problem on screen."""

    severity: str | None
    title: str
    description: str
    metadata: dict[str, Any] = field(default_factory=dict)


async def measure_page(client: httpx.AsyncClient, url: str) -> Measurement:
    """Measure one page — PageSpeed if it answers, raw TTFB if it doesn't."""
    psi = await _fetch_psi(client, url)
    if psi:
        return Measurement(
            url=url, score=psi["score"], lcp=psi["lcp"], cls=psi["cls"],
            fid=psi["fid"], ttfb=psi["ttfb"], source="psi",
        )

    # PSI failed (rate limit / network) — fall back to raw TTFB.
    try:
        ttfb = await _measure_ttfb_fallback(url)
    except Exception as exc:
        return Measurement(url=url, source="error", error=str(exc))

    # A TTFB-only estimate does NOT get fabricated LCP/CLS: the zeros render
    # as "—" instead of polluting Core Web Vitals trends with invented data.
    return Measurement(url=url, score=_score_from_ttfb(ttfb), ttfb=ttfb, source="ttfb")


def classify(m: Measurement) -> Verdict:
    """Turn a measurement into the alert it justifies.

    Shared by the scheduled sweep and the manual re-measure. Two copies of
    these thresholds and this wording would drift, and then the same page
    would read differently depending on which one last touched it.
    """
    if m.error is not None:
        return Verdict(
            "critical",
            f"Page unreachable — {m.url}",
            f"Could not connect: {m.error}",
            {"page_url": m.url, "speed_score": 0, "strategy": "desktop"},
        )

    grade = _score_grade(m.score)
    metadata = {
        "page_url": m.url,
        "speed_score": m.score,
        "lcp_ms": round(m.lcp),
        "cls": round(m.cls, 3),
        "fid_ms": round(m.fid),
        "ttfb_ms": round(m.ttfb),
        "grade": grade,
        "strategy": "desktop",
        "source": m.source,
    }

    if m.source == "psi":
        description = (
            f"PSI desktop score: {m.score}/100 ({grade}). "
            f"LCP: {m.lcp/1000:.1f}s, CLS: {m.cls:.2f}, TTFB: {m.ttfb:.0f}ms."
        )
    else:
        description = (
            f"Estimated score: {m.score}/100 ({grade}) from TTFB {m.ttfb:.0f}ms "
            "— PageSpeed Insights was unavailable for this run."
        )

    if m.score < 50:
        return Verdict("critical", f"Poor desktop performance — {m.url}", description, metadata)
    if m.score < 90:
        return Verdict(
            "warning", f"Desktop performance needs improvement — {m.url}", description, metadata
        )
    return Verdict(None, f"Desktop performance is good — {m.url}", description, metadata)


def snapshot_for(site_id: str, m: Measurement) -> PerformanceSnapshot:
    """The stored history row for a measurement. Unreachable pages get none —
    there is no number to record, and a zero would read as "measured 0"."""
    return PerformanceSnapshot(
        site_id=site_id,
        page_url=m.url,
        lcp=m.lcp,
        cls=m.cls,
        fid=m.fid,
        ttfb=m.ttfb,
        speed_score=m.score,
        strategy="desktop",
    )


def psi_concurrency() -> int:
    """Simultaneous PageSpeed calls this deployment can afford.

    Keyless PSI allows ~25 requests/100s per IP, so more parallelism just
    earns 429s and a run of TTFB estimates instead of scores.
    """
    return (
        settings.PSI_CONCURRENCY_WITH_KEY if settings.PSI_API_KEY
        else settings.PSI_CONCURRENCY
    )


def plan_batch(
    home_url: str,
    candidates: list[str],
    last_seen: dict[str, datetime],
    fresh_cutoff: datetime,
    *,
    budget: int,
) -> tuple[list[str], list[str], list[str]]:
    """(chosen, due, pool) for one measurement run.

    Coverage comes from rotation, not from breadth per run. The previous
    selection took the homepage plus the three highest-traffic posts every
    time, so the same handful of pages were measured forever — 22 of 2,492
    on the install this was found on — while everything else was never seen.

    Least-recently-measured first, never-measured ahead of everything, with
    the incoming traffic order surviving as the tie-break so the busiest of
    two equally stale pages goes first. The homepage always takes a slot: it
    is the page most likely to be looked at, and the one a regression matters
    most on.
    """
    home = home_url.rstrip("/")
    pool = [u for u in dict.fromkeys(candidates) if u and u.rstrip("/") != home]

    far_past = datetime.min.replace(tzinfo=UTC)
    pool.sort(key=lambda u: last_seen.get(u) or far_past)
    due = [u for u in pool if (last_seen.get(u) or far_past) < fresh_cutoff]

    return [home_url, *due[: max(0, budget - 1)]], due, pool


class PerformanceMonitor(BaseAgent):
    async def _select_pages(self, site: Site) -> list[str]:
        """Which pages to measure this run.

        The old selection was the homepage plus the three highest-traffic
        posts, chosen fresh every run — which meant the same handful of pages
        were measured forever and the rest of the library was never seen at
        all. On this install that was 22 pages out of 2,492.

        Coverage now comes from rotation rather than from breadth per run:
        each run takes a bounded slice, least-recently-measured first, so a
        run stays short no matter how large the site is and every page comes
        round in turn. The homepage is always included — it is the page most
        likely to be looked at and the one a regression matters most on.
        """
        fresh_cutoff = datetime.now(UTC) - timedelta(hours=settings.PSI_FRESH_HOURS)

        # Last time each page was measured, for ordering.
        seen_rows = (await self.db.execute(
            select(
                PerformanceSnapshot.page_url,
                func.max(PerformanceSnapshot.snapshot_at).label("last_seen"),
            )
            .where(PerformanceSnapshot.site_id == site.id)
            .group_by(PerformanceSnapshot.page_url)
        )).all()
        last_seen = dict(seen_rows)

        candidates = (await self.db.execute(
            select(ContentPost.url)
            .where(ContentPost.site_id == site.id, ContentPost.url.isnot(None))
            .order_by(ContentPost.traffic_30d.desc())
        )).scalars().all()

        chosen, due, pool = plan_batch(
            site.url, list(candidates), last_seen, fresh_cutoff,
            budget=max(1, settings.PSI_MAX_PAGES_PER_RUN),
        )

        logger.info(
            "PerformanceMonitor %s: %d page(s) this run — %d of %d tracked pages "
            "are due for measurement (%d never measured)",
            site.url, len(chosen), len(due), len(pool),
            sum(1 for u in pool if u not in last_seen),
        )
        return list(dict.fromkeys(chosen))

    async def run(self, site_id: str) -> list[Alert]:
        result = await self.db.execute(select(Site).where(Site.id == site_id))
        site = result.scalar_one_or_none()
        if not site:
            return []

        urls = await self._select_pages(site)
        if not urls:
            return []

        # Existing perf alerts keyed by page URL — reconciled after measuring
        # Read what the rest of the run needs BEFORE releasing the connection.
        # rollback() expires every ORM object regardless of expire_on_commit,
        # so touching `site` afterwards would trigger a lazy refresh from a
        # context that cannot await one.
        site_url = site.url

        # A PageSpeed sweep is minutes of network time. Holding a pooled
        # database connection across it is what turns a slow run into a slow
        # system: the pool is small, and a connection parked on a socket wait
        # is one no request can have. Nothing has been written yet, so this
        # just ends the read transaction and hands the connection back; the
        # writes below re-acquire one when they actually need it.
        await self.db.rollback()

        alerts: list[Alert] = []
        # URLs actually verified this run. Only these may be reconciled — see
        # the note where stale alerts are resolved.
        measured: set[str] = set()
        semaphore = asyncio.Semaphore(psi_concurrency())

        # ── Phase 1: measurements — concurrent, one pooled client ────────────
        async def measure_url(client: httpx.AsyncClient, url: str) -> Measurement:
            async with semaphore:
                return await measure_page(client, url)

        async with httpx.AsyncClient(
            limits=httpx.Limits(max_connections=4, max_keepalive_connections=2),
        ) as client:
            measurements = await asyncio.gather(*[measure_url(client, u) for u in urls])

        existing_r = await self.db.execute(
            select(Alert).where(
                Alert.site_id == site_id,
                Alert.agent == "watchdog",
                Alert.type == "performance",
            )
        )
        existing_by_url: dict[str, Alert] = {}
        stale_ids: list[str] = []
        for a in existing_r.scalars().all():
            url_key = (a.metadata_ or {}).get("page_url", "")
            if url_key and url_key not in existing_by_url:
                existing_by_url[url_key] = a
            else:
                stale_ids.append(a.id)

        # Trim old snapshots so the table can't grow without bound
        await self.db.execute(
            delete(PerformanceSnapshot).where(
                PerformanceSnapshot.site_id == site_id,
                PerformanceSnapshot.snapshot_at
                < datetime.now(UTC) - timedelta(days=SNAPSHOT_RETENTION_DAYS),
            )
        )


        # ── Phase 2: DB writes — sequential (AsyncSession is not concurrency-safe)
        for m in measurements:
            measured.add(m.url)
            verdict = classify(m)

            if m.error is None:
                self.db.add(snapshot_for(site_id, m))

            if verdict.severity is None:
                # Healthy again — clear any previous alert for this page
                current = existing_by_url.pop(m.url, None)
                if current:
                    stale_ids.append(current.id)
                continue

            current = existing_by_url.pop(m.url, None)
            if current:
                # Dismissed/acknowledged status and created_at survive, but a
                # climb into "critical" still notifies.
                await self.update_alert(
                    current, severity=verdict.severity, title=verdict.title,
                    description=verdict.description, metadata=verdict.metadata,
                )
            else:
                alerts.append(await self.create_alert(
                    site_id=site_id, agent="watchdog", severity=verdict.severity,
                    type_="performance", title=verdict.title,
                    description=verdict.description, metadata=verdict.metadata,
                ))

        # Alerts for pages this run did NOT measure are deliberately left
        # alone. The measured set is only the homepage plus the top 3 posts by
        # traffic, so that ranking shifts constantly — deleting the leftovers
        # meant a page's real performance alert vanished the moment a busier
        # post displaced it, with nothing actually fixed. LinkChecker already
        # holds this line ("a link we didn't verify is not fixed"); the two
        # agents disagreeing was the bug.
        if stale_ids:
            await self.db.execute(delete(Alert).where(Alert.id.in_(set(stale_ids))))

        logger.info(
            "PerformanceMonitor %s: measured %d/%d pages, %d alert(s) kept for pages "
            "not measured this run",
            site_url, len(measured), len(urls), len(existing_by_url),
        )
        await self.db.flush()
        return alerts
