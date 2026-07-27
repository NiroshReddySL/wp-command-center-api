"""
Config Drift Detector — compares current WP config against a stored baseline.

Algorithm:
1. On first run: fetch WP site settings via REST API and store as baseline JSON
2. On subsequent runs: fetch current settings and deep-diff against baseline
3. Drifted settings (changed user roles, debug mode on, file editing enabled):
   create critical/warning alerts depending on severity
4. Update baseline after user acknowledges drift
"""
from app.agents.base import BaseAgent
from app.database.models import Alert


class ConfigDriftDetector(BaseAgent):
    async def run(self, site_id: str) -> list[Alert]:
        # Implementation: fetch settings, compare to baseline, alert on drift
        return []
