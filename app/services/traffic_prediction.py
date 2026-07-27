"""Traffic Prediction Service — caches GPT-4o forecasts in the traffic_predictions table."""
import logging
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.engine import ai_engine
from app.ai.prompts import traffic_prediction_prompt
from app.database.models import Site, TrafficPrediction, TrafficSnapshot

logger = logging.getLogger(__name__)

HISTORY_DAYS = 90
CACHE_TTL_HOURS = 24
MIN_SNAPSHOTS = 14  # need at least 2 weeks of data to forecast


class TrafficPredictionService:
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def get_or_generate(
        self,
        site_id: str,
        horizon_days: int = 7,
        force: bool = False,
    ) -> TrafficPrediction | None:
        # Check cache
        if not force:
            cached = await self._fetch_cached(site_id, horizon_days)
            if cached:
                return cached

        # Fetch history
        snapshots = await self._fetch_history(site_id)
        if len(snapshots) < MIN_SNAPSHOTS:
            logger.info("Site %s has only %d snapshots — skipping prediction", site_id, len(snapshots))
            return None

        # Get site name
        site_r = await self.db.execute(select(Site).where(Site.id == site_id))
        site = site_r.scalar_one_or_none()
        site_name = site.name if site else site_id

        return await self._generate(site_id, site_name, horizon_days, snapshots)

    async def _fetch_cached(self, site_id: str, horizon_days: int) -> TrafficPrediction | None:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=CACHE_TTL_HOURS)
        result = await self.db.execute(
            select(TrafficPrediction)
            .where(
                TrafficPrediction.site_id == site_id,
                TrafficPrediction.horizon_days == horizon_days,
                TrafficPrediction.generated_at >= cutoff,
            )
            .order_by(TrafficPrediction.generated_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _fetch_history(self, site_id: str) -> list[TrafficSnapshot]:
        since = (datetime.now(timezone.utc) - timedelta(days=HISTORY_DAYS)).strftime("%Y-%m-%d")
        result = await self.db.execute(
            select(TrafficSnapshot)
            .where(TrafficSnapshot.site_id == site_id, TrafficSnapshot.date >= since)
            .order_by(TrafficSnapshot.date.asc())
        )
        return list(result.scalars().all())

    def _build_csv(self, snapshots: list[TrafficSnapshot]) -> str:
        lines = ["date,pageviews,sessions,users"]
        for s in snapshots:
            lines.append(f"{s.date},{s.pageviews},{s.sessions},{s.users}")
        return "\n".join(lines)

    def _validate_forecasts(self, forecasts: list[dict], horizon_days: int) -> list[dict]:
        """Re-index forecast dates to be consecutive from tomorrow, in case GPT drifts."""
        base_date = datetime.now(timezone.utc).date() + timedelta(days=1)
        validated = []
        for i in range(horizon_days):
            expected_date = (base_date + timedelta(days=i)).isoformat()
            if i < len(forecasts):
                entry = forecasts[i]
                validated.append({
                    "date": expected_date,
                    "base": max(0, int(entry.get("base", 0))),
                    "optimistic": max(0, int(entry.get("optimistic", 0))),
                    "pessimistic": max(0, int(entry.get("pessimistic", 0))),
                })
            else:
                # GPT returned fewer rows — extrapolate last value
                last = validated[-1] if validated else {"base": 0, "optimistic": 0, "pessimistic": 0}
                validated.append({"date": expected_date, **{k: last[k] for k in ["base", "optimistic", "pessimistic"]}})
        return validated

    async def _generate(
        self,
        site_id: str,
        site_name: str,
        horizon_days: int,
        snapshots: list[TrafficSnapshot],
    ) -> TrafficPrediction:
        history_csv = self._build_csv(snapshots)
        prompt = traffic_prediction_prompt(site_name, history_csv, horizon_days)

        try:
            raw: dict[str, Any] = await ai_engine.generate_json(prompt, max_tokens=4096)
        except Exception as exc:
            logger.error("GPT-4o prediction failed for site %s: %s", site_id, exc)
            raw = {}

        forecasts = self._validate_forecasts(raw.get("daily_forecasts", []), horizon_days)
        anomalies = raw.get("anomalies", [])
        narrative = raw.get("narrative", "Prediction generated from historical traffic data.")

        # Replace the old prediction only after generation succeeds, and BEFORE
        # inserting the new row — deleting afterwards would match and wipe it.
        await self.db.execute(
            delete(TrafficPrediction)
            .where(TrafficPrediction.site_id == site_id, TrafficPrediction.horizon_days == horizon_days)
        )

        prediction = TrafficPrediction(
            site_id=site_id,
            horizon_days=horizon_days,
            daily_forecasts=forecasts,
            anomalies=anomalies,
            narrative=narrative,
            model_version="gpt-4o",
        )
        self.db.add(prediction)
        await self.db.flush()
        return prediction
