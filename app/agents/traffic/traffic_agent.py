"""Traffic Agent — pulls GA4 daily metrics (or estimates from post data) and stores TrafficSnapshot rows.

Runs daily. Detects:
  - traffic_drop       : day-over-day drop > 20%  → warning; > 50% → critical
  - traffic_spike      : day-over-day spike > 100% → info (good signal for Autopilot)
  - high_bounce_rate   : bounce rate > 70%         → warning
  - low_engagement     : avg session < 30s         → warning

When a GA4-connected site's history is thin (or has leftover "estimated"
days from before GA4 was connected), `run()` first does a one-shot
historical backfill straight from GA4 (see backfill_from_ga4) — GA4
already has that history, so there's no reason to wait for this agent to
accumulate it one calendar day at a time before predictions become
possible.
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

from app.agents.base import BaseAgent
from app.database.models import Alert, ContentPost, SiteConfig, TrafficSnapshot

logger = logging.getLogger(__name__)

# Mirrors TrafficPredictionService.HISTORY_DAYS — a backfill only ever needs
# to cover as much history as a forecast would ever look at.
BACKFILL_LOOKBACK_DAYS = 90


def _previous_day(date_str: str) -> str:
    return (datetime.strptime(date_str, "%Y-%m-%d") - timedelta(days=1)).strftime("%Y-%m-%d")


def _should_write_snapshot(existing_source: str | None, new_source: str) -> bool:
    """Whether new snapshot data for a date should overwrite what's already
    stored. GA4 data is authoritative and must never be downgraded back to
    a rough estimate once obtained — every other case (nothing stored yet,
    or refreshing a same-source day) is fine to write."""
    if existing_source is None:
        return True
    return not (existing_source == "ga4" and new_source == "estimated")


def _should_attempt_backfill(sources: list[str], min_snapshots: int) -> bool:
    """Whether a GA4 historical backfill is worth the extra API call —
    either there's a real gap (fewer real GA4 days than a forecast needs),
    or an "estimated" day sits in the window that GA4 could now upgrade.
    Once history is fully real and complete, this is a no-op skip rather
    than an extra GA4 call on every single nightly run forever."""
    ga4_count = sum(1 for s in sources if s == "ga4")
    return ga4_count < min_snapshots or "estimated" in sources


class TrafficAgent(BaseAgent):
    AGENT = "traffic"

    async def run(self, site_id: str) -> list[Alert]:
        alerts: list[Alert] = []

        if await self._is_ga4_connected(site_id):
            if await self._needs_backfill(site_id):
                await self.backfill_from_ga4(site_id)
            snapshot = await self._fetch_ga4(site_id)
            # Connected but THIS run's fetch failed (rate limit, network
            # blip, expired-token race) — leave no snapshot for today rather
            # than silently mislabeling it "estimated". Yesterday's real
            # GA4 snapshot remains the most recent good data, and the
            # dashboard's own staleness check surfaces the gap honestly.
            if snapshot is None:
                return alerts
        else:
            snapshot = await self._estimate_from_posts(site_id)
            if snapshot is None:
                return alerts

        await self._upsert_snapshot(site_id, snapshot)
        await self.db.flush()

        # ── Compare with the previous day's snapshot ──────────────────────────
        # Derived from the snapshot's OWN date, not independently recomputed
        # from "now" — GA4 snapshots are always dated yesterday (GA4 needs a
        # full day to finalize), so re-deriving "yesterday" from "now" here
        # would resolve to that SAME date and compare the row against
        # itself (always 0% change, no alert ever fires).
        prev_r = await self.db.execute(
            select(TrafficSnapshot)
            .where(TrafficSnapshot.site_id == site_id, TrafficSnapshot.date == _previous_day(snapshot["date"]))
        )
        prev = prev_r.scalar_one_or_none()

        if prev and prev.pageviews > 0:
            change_pct = ((snapshot["pageviews"] - prev.pageviews) / prev.pageviews) * 100

            if change_pct <= -50:
                alerts.append(await self.create_alert(
                    site_id=site_id, agent=self.AGENT, severity="critical",
                    type_="traffic_drop",
                    title=f"Traffic crashed — down {abs(change_pct):.0f}% vs yesterday",
                    description=f"Pageviews dropped from {prev.pageviews:,} to {snapshot['pageviews']:,}.",
                    metadata={
                        "pageviews_today": snapshot["pageviews"],
                        "pageviews_yesterday": prev.pageviews,
                        "change_pct": round(change_pct, 1),
                        "source": snapshot["source"],
                    },
                ))
            elif change_pct <= -20:
                alerts.append(await self.create_alert(
                    site_id=site_id, agent=self.AGENT, severity="warning",
                    type_="traffic_drop",
                    title=f"Traffic declined {abs(change_pct):.0f}% vs yesterday",
                    description=f"Pageviews: {prev.pageviews:,} → {snapshot['pageviews']:,}.",
                    metadata={
                        "pageviews_today": snapshot["pageviews"],
                        "pageviews_yesterday": prev.pageviews,
                        "change_pct": round(change_pct, 1),
                        "source": snapshot["source"],
                    },
                ))
            elif change_pct >= 100:
                alerts.append(await self.create_alert(
                    site_id=site_id, agent=self.AGENT, severity="info",
                    type_="traffic_spike",
                    title=f"Traffic spike — up {change_pct:.0f}% vs yesterday",
                    description=f"Pageviews jumped from {prev.pageviews:,} to {snapshot['pageviews']:,}.",
                    metadata={
                        "pageviews_today": snapshot["pageviews"],
                        "pageviews_yesterday": prev.pageviews,
                        "change_pct": round(change_pct, 1),
                        "top_pages": snapshot["top_pages"][:3],
                        "source": snapshot["source"],
                    },
                ))

        # ── Engagement alerts ──────────────────────────────────────────────────
        if snapshot["bounce_rate"] > 70 and snapshot["pageviews"] > 100:
            alerts.append(await self.create_alert(
                site_id=site_id, agent=self.AGENT, severity="warning",
                type_="high_bounce_rate",
                title=f"High bounce rate — {snapshot['bounce_rate']:.1f}%",
                description="More than 70% of visitors leave without engaging.",
                metadata={
                    "bounce_rate": snapshot["bounce_rate"],
                    "pageviews": snapshot["pageviews"],
                    "source": snapshot["source"],
                },
            ))

        if 0 < snapshot["avg_session_duration"] < 30 and snapshot["sessions"] > 50:
            alerts.append(await self.create_alert(
                site_id=site_id, agent=self.AGENT, severity="warning",
                type_="low_engagement",
                title=f"Low engagement — avg session {snapshot['avg_session_duration']:.0f}s",
                description="Visitors are leaving quickly. Content may not match search intent.",
                metadata={
                    "avg_session_duration": snapshot["avg_session_duration"],
                    "sessions": snapshot["sessions"],
                    "source": snapshot["source"],
                },
            ))

        return alerts

    # ── Persistence ───────────────────────────────────────────────────────────

    async def _upsert_snapshot(self, site_id: str, snapshot: dict[str, Any]) -> bool:
        """Write one day's snapshot — updates the existing row for this
        (site_id, date) if one exists (uq_traffic_snapshots_site_date),
        inserts a new one otherwise. Never downgrades a real GA4 row back
        to an estimate (_should_write_snapshot). Returns whether a write
        actually happened."""
        existing_r = await self.db.execute(
            select(TrafficSnapshot)
            .where(TrafficSnapshot.site_id == site_id, TrafficSnapshot.date == snapshot["date"])
        )
        existing = existing_r.scalar_one_or_none()

        if not _should_write_snapshot(existing.source if existing else None, snapshot["source"]):
            return False

        if existing is not None:
            existing.pageviews = snapshot["pageviews"]
            existing.sessions = snapshot["sessions"]
            existing.users = snapshot["users"]
            existing.bounce_rate = snapshot["bounce_rate"]
            existing.avg_session_duration = snapshot["avg_session_duration"]
            existing.top_pages = snapshot["top_pages"]
            existing.geo_countries = snapshot.get("geo_countries", existing.geo_countries)
            existing.geo_regions = snapshot.get("geo_regions", existing.geo_regions)
            existing.geo_cities = snapshot.get("geo_cities", existing.geo_cities)
            existing.source = snapshot["source"]
            existing.snapshot_at = datetime.now(timezone.utc)
        else:
            self.db.add(TrafficSnapshot(
                site_id=site_id,
                date=snapshot["date"],
                pageviews=snapshot["pageviews"],
                sessions=snapshot["sessions"],
                users=snapshot["users"],
                bounce_rate=snapshot["bounce_rate"],
                avg_session_duration=snapshot["avg_session_duration"],
                top_pages=snapshot["top_pages"],
                geo_countries=snapshot.get("geo_countries", []),
                geo_regions=snapshot.get("geo_regions", []),
                geo_cities=snapshot.get("geo_cities", []),
                source=snapshot["source"],
            ))
        return True

    # ── Data sources ──────────────────────────────────────────────────────────

    async def _is_ga4_connected(self, site_id: str) -> bool:
        """Whether this site has a real, usable GA4 connection — checked
        BEFORE attempting a fetch so a transient failure of the actual GA4
        call (below) is never confused with "GA4 isn't connected"."""
        from app.api.auth import get_google_token

        token = await get_google_token(self.db)
        if not token:
            return False
        cfg_r = await self.db.execute(select(SiteConfig).where(SiteConfig.site_id == site_id))
        cfg = cfg_r.scalar_one_or_none()
        return bool(cfg and cfg.ga_property_id)

    async def _needs_backfill(self, site_id: str) -> bool:
        """A cheap DB-only check (no GA4 call) deciding whether
        backfill_from_ga4 is worth running this time — see
        _should_attempt_backfill for the actual decision."""
        from app.services.traffic_prediction import MIN_SNAPSHOTS

        since = (datetime.now(timezone.utc) - timedelta(days=BACKFILL_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
        result = await self.db.execute(
            select(TrafficSnapshot.source)
            .where(TrafficSnapshot.site_id == site_id, TrafficSnapshot.date >= since)
        )
        sources = [s for (s,) in result.all()]
        return _should_attempt_backfill(sources, MIN_SNAPSHOTS)

    async def backfill_from_ga4(self, site_id: str, days: int = BACKFILL_LOOKBACK_DAYS) -> int:
        """One-shot historical backfill straight from GA4, in a single API
        call — a site's history already exists there, so there's no reason
        to wait for the nightly agent to accumulate `days` calendar days
        one at a time before a forecast becomes possible. Also upgrades any
        "estimated" day to real GA4 numbers wherever GA4 has data for that
        date (never the reverse — see _should_write_snapshot).

        Backfilled days don't get a top_pages/geo breakdown (that would be
        a separate GA4 call PER historical day) — only pageviews/sessions/
        users/bounce_rate/avg_session_duration, which is everything a
        forecast actually needs. The single most recent day still gets the
        full breakdown via the normal _fetch_ga4 path that runs right after
        this in `run()`. Returns how many days were written or upgraded.
        """
        if not await self._is_ga4_connected(site_id):
            return 0

        from app.api.auth import get_google_token
        from app.connectors.analytics import AnalyticsConnector

        token = await get_google_token(self.db)
        cfg_r = await self.db.execute(select(SiteConfig).where(SiteConfig.site_id == site_id))
        cfg = cfg_r.scalar_one_or_none()
        if not token or not cfg or not cfg.ga_property_id:
            return 0

        try:
            ga = AnalyticsConnector(token.access_token)
            daily = await ga.get_daily_site_metrics(cfg.ga_property_id, days=days)
        except Exception as exc:
            logger.warning("GA4 historical backfill failed for site %s: %s", site_id, exc)
            return 0

        written = 0
        for day in daily:
            wrote = await self._upsert_snapshot(site_id, {**day, "top_pages": [], "source": "ga4"})
            if wrote:
                written += 1
        await self.db.flush()
        return written

    async def _fetch_ga4(self, site_id: str) -> dict[str, Any] | None:
        """Pull yesterday's metrics from GA4. Caller has already confirmed
        this site IS connected (_is_ga4_connected) — a None return here
        means this particular call failed (rate limit, network, an expired
        token race), not that GA4 is absent."""
        try:
            from app.api.auth import get_google_token
            from app.connectors.analytics import AnalyticsConnector

            token = await get_google_token(self.db)
            if not token:
                return None

            cfg_r = await self.db.execute(select(SiteConfig).where(SiteConfig.site_id == site_id))
            cfg = cfg_r.scalar_one_or_none()
            if not cfg or not cfg.ga_property_id:
                return None

            ga = AnalyticsConnector(token.access_token)
            metrics, top_pages, geo = await asyncio.gather(
                ga.get_site_metrics(cfg.ga_property_id, days=1),
                ga.get_top_pages(cfg.ga_property_id, days=1, limit=10),
                ga.get_geo_breakdown(cfg.ga_property_id, days=1),
            )

            yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
            return {
                "date": yesterday,
                "pageviews": metrics.get("pageviews", 0),
                "sessions": metrics.get("sessions", 0),
                "users": metrics.get("users", 0),
                "bounce_rate": metrics.get("bounce_rate", 0.0),
                "avg_session_duration": metrics.get("avg_session_duration", 0.0),
                "top_pages": top_pages,
                "geo_countries": geo.get("countries", []),
                "geo_regions": geo.get("regions", []),
                "geo_cities": geo.get("cities", []),
                "source": "ga4",
            }
        except Exception as exc:
            logger.warning("GA4 fetch failed for connected site %s: %s", site_id, exc)
            return None

    async def _estimate_from_posts(self, site_id: str) -> dict[str, Any] | None:
        """Estimate daily traffic from sum of ContentPost.traffic_30d / 30."""
        result = await self.db.execute(
            select(ContentPost)
            .where(ContentPost.site_id == site_id)
            .order_by(ContentPost.traffic_30d.desc())
        )
        posts = result.scalars().all()
        if not posts:
            return None

        total_30d = sum(p.traffic_30d for p in posts)
        estimated_daily = total_30d // 30

        top_pages = [
            {"url": p.url, "title": p.title, "views": p.traffic_30d // 30}
            for p in posts[:5]
            if p.traffic_30d > 0
        ]

        return {
            "date": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
            "pageviews": estimated_daily,
            "sessions": int(estimated_daily * 0.75),
            "users": int(estimated_daily * 0.65),
            "bounce_rate": 0.0,
            "avg_session_duration": 0.0,
            "top_pages": top_pages,
            "source": "estimated",
        }
