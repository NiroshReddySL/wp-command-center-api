"""Flow Classifier — evaluates every active FlowCategory's ordered
page-pattern steps against GA4's Funnel Reports API and stores a daily
snapshot, building up the trend history a dashboard needs one day at a
time (GA4 itself has no concept of "yesterday's funnel result" persisted
anywhere — every query is a fresh, on-demand computation).
"""
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import selectinload

from app.agents.base import BaseAgent
from app.database.models import Alert, FlowCategory, FlowCategorySnapshot, SiteConfig

logger = logging.getLogger(__name__)

# Below this many entrants, a day-over-day conversion-rate swing is just
# small-sample noise, not a real signal worth alerting on.
_MIN_SAMPLE_FOR_ALERT = 20
_CONVERSION_DROP_THRESHOLD = -0.3  # relative drop, e.g. -0.3 = conversion rate fell 30%


def _drop_ratio_if_alertworthy(
    total_entered: int, conversion_rate: float,
    prev_total_entered: int, prev_conversion_rate: float,
) -> float | None:
    """Returns the relative change (always negative when returned — a
    drop) if it crosses the alert threshold with a meaningful sample on
    both sides, else None. Pulled out as a pure function so the threshold
    math is directly testable without a DB session."""
    if total_entered < _MIN_SAMPLE_FOR_ALERT or prev_total_entered < _MIN_SAMPLE_FOR_ALERT:
        return None
    if prev_conversion_rate <= 0:
        return None
    change = (conversion_rate - prev_conversion_rate) / prev_conversion_rate
    return change if change <= _CONVERSION_DROP_THRESHOLD else None


def _step_specs(category: FlowCategory) -> list[dict]:
    return [
        {
            "label": s.label, "match_type": s.match_type, "pattern": s.pattern,
            "is_directly_followed": s.is_directly_followed, "within_seconds": s.within_seconds,
        }
        for s in category.steps
    ]


class FlowClassifier(BaseAgent):
    AGENT = "flows"

    async def run(self, site_id: str) -> list[Alert]:
        alerts: list[Alert] = []

        cfg_r = await self.db.execute(select(SiteConfig).where(SiteConfig.site_id == site_id))
        cfg = cfg_r.scalar_one_or_none()
        if not cfg or not cfg.ga_property_id:
            return alerts

        categories_r = await self.db.execute(
            select(FlowCategory)
            .options(selectinload(FlowCategory.steps))
            .where(FlowCategory.site_id == site_id, FlowCategory.is_active.is_(True))
        )
        categories = categories_r.scalars().all()
        if not categories:
            return alerts

        from app.api.auth import get_google_token
        from app.connectors.analytics import AnalyticsConnector

        token = await get_google_token(self.db)
        if not token:
            return alerts

        ga = AnalyticsConnector(token.access_token)
        yesterday = (datetime.now(timezone.utc) - timedelta(days=1)).date().isoformat()

        for category in categories:
            steps = _step_specs(category)
            if not steps:
                continue
            try:
                result = await ga.run_funnel_report(cfg.ga_property_id, steps, yesterday, yesterday)
            except Exception as exc:
                logger.warning(
                    "FlowClassifier: funnel query failed for category %s (site %s): %s",
                    category.id, site_id, exc,
                )
                continue

            snapshot = FlowCategorySnapshot(
                flow_category_id=category.id, site_id=site_id,
                range_start=yesterday, range_end=yesterday,
                step_results=result["step_results"],
                total_entered=result["total_entered"],
                total_completed=result["total_completed"],
                conversion_rate=result["conversion_rate"],
            )
            self.db.add(snapshot)
            await self.db.flush()

            alert = await self._check_conversion_drop(site_id, category, result, yesterday)
            if alert:
                alerts.append(alert)

        return alerts

    async def _check_conversion_drop(
        self, site_id: str, category: FlowCategory, result: dict, today: str,
    ) -> Alert | None:
        """Day-over-day comparison against the prior single-day snapshot —
        same "meaningful sample + relative threshold" shape as TrafficAgent's
        traffic_drop check, just applied to a flow's conversion rate."""
        prev_r = await self.db.execute(
            select(FlowCategorySnapshot)
            .where(
                FlowCategorySnapshot.flow_category_id == category.id,
                FlowCategorySnapshot.range_start == FlowCategorySnapshot.range_end,
                FlowCategorySnapshot.range_start < today,
            )
            .order_by(FlowCategorySnapshot.range_start.desc())
            .limit(1)
        )
        prev = prev_r.scalar_one_or_none()
        if not prev:
            return None

        change = _drop_ratio_if_alertworthy(
            result["total_entered"], result["conversion_rate"], prev.total_entered, prev.conversion_rate,
        )
        if change is None:
            return None

        return await self.create_alert(
            site_id=site_id, agent=self.AGENT, severity="warning",
            type_="flow_conversion_drop",
            title=f'"{category.name}" conversion dropped {abs(change) * 100:.0f}%',
            description=(
                f"Conversion rate fell from {prev.conversion_rate * 100:.1f}% to "
                f"{result['conversion_rate'] * 100:.1f}% day-over-day "
                f"({prev.total_entered:,} → {result['total_entered']:,} entrants)."
            ),
            metadata={
                "flow_category_id": category.id,
                "flow_category_name": category.name,
                "conversion_rate_today": result["conversion_rate"],
                "conversion_rate_yesterday": prev.conversion_rate,
                "total_entered_today": result["total_entered"],
                "total_entered_yesterday": prev.total_entered,
            },
        )
