"""Traffic Agent — pulls GA4 daily metrics (or estimates from post data) and stores TrafficSnapshot rows.

Runs daily. Detects:
  - traffic_drop       : day-over-day drop > 20%  → warning; > 50% → critical
  - traffic_spike      : day-over-day spike > 100% → info (good signal for Autopilot)
  - high_bounce_rate   : bounce rate > 70%         → warning
  - low_engagement     : avg session < 30s         → warning
"""

import asyncio
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select

from app.agents.base import BaseAgent
from app.database.models import Alert, ContentPost, SiteConfig, TrafficSnapshot

logger = logging.getLogger(__name__)


class TrafficAgent(BaseAgent):
    AGENT = "traffic"

    async def run(self, site_id: str) -> list[Alert]:
        alerts: list[Alert] = []

        # Try GA4 first
        snapshot = await self._fetch_ga4(site_id)
        if snapshot is None:
            snapshot = await self._estimate_from_posts(site_id)

        if snapshot is None:
            return alerts

        # Persist snapshot
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
        await self.db.flush()

        # ── Compare with yesterday's snapshot ─────────────────────────────────
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).strftime("%Y-%m-%d")
        prev_r = await self.db.execute(
            select(TrafficSnapshot)
            .where(TrafficSnapshot.site_id == site_id, TrafficSnapshot.date == yesterday)
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

    # ── Data sources ──────────────────────────────────────────────────────────

    async def _fetch_ga4(self, site_id: str) -> dict[str, Any] | None:
        """Pull yesterday's metrics from GA4 if the site has a property configured."""
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
            logger.debug("GA4 fetch failed for site %s: %s", site_id, exc)
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
