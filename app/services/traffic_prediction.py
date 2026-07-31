"""Traffic Prediction Service — caches GPT-4o forecasts in the traffic_predictions table."""
import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.ai.engine import ai_engine
from app.ai.prompts import traffic_prediction_prompt
from app.database.models import Site, SiteConfig, TrafficPrediction, TrafficSnapshot

logger = logging.getLogger(__name__)

HISTORY_DAYS = 90
CACHE_TTL_HOURS = 24
MIN_SNAPSHOTS = 14  # need at least 2 weeks of data to forecast


class PredictionGenerationFailed(Exception):
    """Raised when the AI call itself failed and there's no usable cached
    prediction to fall back to — distinct from "insufficient data" (there
    just isn't enough history yet), which is a None return, not an error."""


def _build_history_csv(
    snapshots: list[TrafficSnapshot], search_by_date: dict[str, dict[str, Any]],
) -> str:
    """GA4 traffic joined to Search Console performance, one row per day.

    The search columns are only added when there's actually GSC data —
    a site with no verified Search Console property gets exactly the
    traffic-only CSV this produced before, so the forecast degrades to its
    previous behaviour rather than breaking.

    A day GSC hasn't finalized yet (it runs ~2-3 days behind GA4) is left
    BLANK, never zero: zeros would read to the model as "search traffic
    collapsed to nothing" on precisely the most recent — and most heavily
    weighted — days of the series.
    """
    if not search_by_date:
        lines = ["date,pageviews,sessions,users"]
        for s in snapshots:
            lines.append(f"{s.date},{s.pageviews},{s.sessions},{s.users}")
        return "\n".join(lines)

    lines = ["date,pageviews,sessions,users,impressions,clicks,ctr_pct,avg_position"]
    for s in snapshots:
        search = search_by_date.get(s.date)
        if search is None:
            search_cells = ",,,"
        else:
            search_cells = (
                f"{search['impressions']},{search['clicks']},"
                f"{search['ctr']},{search['position']}"
            )
        lines.append(f"{s.date},{s.pageviews},{s.sessions},{s.users},{search_cells}")
    return "\n".join(lines)


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

        generated = await self._generate(site_id, site_name, horizon_days, snapshots)
        if generated is not None:
            return generated

        # The AI call itself failed. Serving the last known-good forecast
        # (regardless of its age) is far more honest than fabricating a
        # zero-line "success" — only when nothing has ever been generated
        # for this site+horizon is this a genuine, reportable failure.
        stale = await self._fetch_latest(site_id, horizon_days)
        if stale is not None:
            return stale
        raise PredictionGenerationFailed(f"Prediction generation failed for site {site_id}")

    async def _fetch_latest(self, site_id: str, horizon_days: int) -> TrafficPrediction | None:
        result = await self.db.execute(
            select(TrafficPrediction)
            .where(TrafficPrediction.site_id == site_id, TrafficPrediction.horizon_days == horizon_days)
            .order_by(TrafficPrediction.generated_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    async def _fetch_cached(self, site_id: str, horizon_days: int) -> TrafficPrediction | None:
        latest = await self._fetch_latest(site_id, horizon_days)
        if latest is None:
            return None
        cutoff = datetime.now(UTC) - timedelta(hours=CACHE_TTL_HOURS)
        return latest if latest.generated_at >= cutoff else None

    async def _fetch_history(self, site_id: str) -> list[TrafficSnapshot]:
        since = (datetime.now(UTC) - timedelta(days=HISTORY_DAYS)).strftime("%Y-%m-%d")
        result = await self.db.execute(
            select(TrafficSnapshot)
            .where(TrafficSnapshot.site_id == site_id, TrafficSnapshot.date >= since)
            .order_by(TrafficSnapshot.date.asc())
        )
        return list(result.scalars().all())

    async def _fetch_search_history(self, site_id: str) -> dict[str, dict[str, Any]]:
        """Daily Search Console performance keyed by date, or {} when it
        isn't available for this site.

        Deliberately never raises: Search Console is an ENRICHMENT here.
        A site that was never verified in GSC, a property the connected
        Google account can't read (403), or a transient API failure must
        all degrade to a traffic-only forecast — exactly what this produced
        before — rather than taking down a prediction that GA4 alone can
        perfectly well support.
        """
        from app.api.auth import get_google_token
        from app.connectors.search_console import SearchConsoleConnector

        try:
            token = await get_google_token(self.db)
            if not token:
                return {}

            cfg_r = await self.db.execute(select(SiteConfig).where(SiteConfig.site_id == site_id))
            cfg = cfg_r.scalar_one_or_none()
            site_r = await self.db.execute(select(Site).where(Site.id == site_id))
            site = site_r.scalar_one_or_none()

            # Same resolution order the Optimizer's GSC calls already use.
            gsc_url = (cfg.gsc_site_url if cfg else None) or (site.url if site else None)
            if not gsc_url:
                return {}

            gsc = SearchConsoleConnector(token.access_token)
            daily = await gsc.get_daily_search_metrics(gsc_url, days=HISTORY_DAYS)
        except Exception as exc:
            logger.info(
                "Search Console enrichment unavailable for site %s (forecasting on traffic alone): %s",
                site_id, exc,
            )
            return {}

        return {row["date"]: row for row in daily}

    def _validate_forecasts(self, forecasts: list[dict], horizon_days: int) -> list[dict]:
        """Re-index forecast dates to be consecutive from tomorrow, in case GPT drifts."""
        base_date = datetime.now(UTC).date() + timedelta(days=1)
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
    ) -> TrafficPrediction | None:
        """Returns None (never a fabricated forecast) when the AI call
        itself fails — a flat all-zero prediction previously got saved and
        cached for 24h as if it were a real, successful forecast, which was
        indistinguishable from "working" to a user."""
        search_by_date = await self._fetch_search_history(site_id)
        history_csv = _build_history_csv(snapshots, search_by_date)
        prompt = traffic_prediction_prompt(
            site_name, history_csv, horizon_days, has_search_data=bool(search_by_date),
        )

        try:
            raw: dict[str, Any] = await ai_engine.generate_json(prompt, max_tokens=4096)
        except Exception as exc:
            logger.error("GPT-4o prediction failed for site %s: %s", site_id, exc)
            return None

        # generate_json() also fails "quietly" — it returns {} rather than
        # raising when the model's response wasn't parseable JSON at all.
        # An empty dict here means the call produced nothing usable, same
        # as an outright exception.
        if not raw:
            logger.error("GPT-4o returned an empty/unparseable response for site %s", site_id)
            return None

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
