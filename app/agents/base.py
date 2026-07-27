import logging
from abc import ABC, abstractmethod
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.models import Alert, Site

logger = logging.getLogger(__name__)


class BaseAgent(ABC):
    def __init__(self, db: AsyncSession) -> None:
        self.db = db

    async def create_alert(
        self,
        site_id: str,
        agent: str,
        severity: str,
        type_: str,
        title: str,
        description: str,
        metadata: dict[str, Any] | None = None,
    ) -> Alert:
        alert = Alert(
            site_id=site_id,
            agent=agent,
            severity=severity,
            type=type_,
            title=title,
            description=description,
            metadata_=metadata or {},
            status="open",
        )
        self.db.add(alert)
        await self.db.flush()

        if severity == "critical":
            await self._notify(alert)

        return alert

    async def _notify(self, alert: Alert) -> None:
        """Dispatch critical alerts to configured channels. Never raises."""
        from app.services.notification import dispatch_alert_notification

        try:
            name_r = await self.db.execute(select(Site.name).where(Site.id == alert.site_id))
            site_name = name_r.scalar_one_or_none() or alert.site_id
            await dispatch_alert_notification(self.db, alert, site_name)
        except Exception as exc:
            logger.warning("Alert notification dispatch failed: %s", exc)

    @abstractmethod
    async def run(self, site_id: str) -> list[Alert]:
        """Run the agent against a single site and return created alerts."""

    async def run_all_sites(self, site_ids: list[str]) -> list[Alert]:
        all_alerts: list[Alert] = []
        for site_id in site_ids:
            alerts = await self.run(site_id)
            all_alerts.extend(alerts)
        return all_alerts
