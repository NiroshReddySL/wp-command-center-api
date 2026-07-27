"""Plugin Audit — checks outdated plugins and CVEs via WPScan Vulnerability Database."""
import asyncio
import logging

import httpx
from sqlalchemy import delete, select

from app.agents.base import BaseAgent
from app.config import settings
from app.connectors.retry import request_with_retries
from app.connectors.wordpress import WordPressConnector
from app.database.models import Alert, PluginAudit, Site

logger = logging.getLogger(__name__)


def _extract_slug(plugin: dict) -> str:
    """
    WP REST API returns plugins in different formats across WP versions.
    The most reliable slug source is the `plugin` field (e.g. "akismet/akismet.php")
    — take everything before the first slash.
    Fall back to `textdomain` only if `plugin` is missing.
    """
    raw = plugin.get("plugin", "")
    if raw and "/" in raw:
        return raw.split("/")[0].lower().strip()
    if raw:
        return raw.lower().strip()
    return (plugin.get("textdomain") or "").lower().strip()


async def _fetch_wporg_version(client: httpx.AsyncClient, slug: str) -> str | None:
    """Return latest version from WordPress.org plugin API, or None on failure."""
    try:
        resp = await request_with_retries(
            lambda: client.get(
                f"https://api.wordpress.org/plugins/info/1.0/{slug}.json",
                timeout=8.0,
            ),
            what=f"wp.org {slug}",
        )
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict) and not data.get("error"):
                return data.get("version")
    except Exception as exc:
        logger.warning("wordpress.org version check failed for %s: %s", slug, exc)
    return None


async def _fetch_wpscan_vulns(client: httpx.AsyncClient, slug: str, api_key: str) -> list[dict] | None:
    """Vulnerability dicts for a plugin from WPScan, or None when the lookup
    failed — an outage or bad API key must NOT read as "no known CVEs"."""
    try:
        resp = await request_with_retries(
            lambda: client.get(
                f"https://wpscan.com/api/v3/plugins/{slug}",
                headers={"Authorization": f"Token token={api_key}"},
                timeout=10.0,
            ),
            what=f"WPScan {slug}",
        )
        if resp.status_code == 200:
            data = resp.json()
            plugin_data = data.get(slug, {})
            return plugin_data.get("vulnerabilities", [])
        if resp.status_code == 404:  # plugin unknown to WPScan — genuinely no data
            return []
        logger.warning("WPScan returned HTTP %d for %s — vulnerability status unknown", resp.status_code, slug)
    except Exception as exc:
        logger.warning("WPScan lookup failed for %s — vulnerability status unknown: %s", slug, exc)
    return None


class PluginAuditor(BaseAgent):
    async def run(self, site_id: str) -> list[Alert]:
        result = await self.db.execute(select(Site).where(Site.id == site_id))
        site = result.scalar_one_or_none()
        if not site or not site.api_key:
            return []

        wp = WordPressConnector(site.url, site.api_key)
        try:
            plugins = await wp.get_plugins()
        except Exception as exc:
            # Silent [] would render as "all plugins healthy" — fail loudly instead.
            raise RuntimeError(f"Could not fetch plugins from {site.url}: {exc}") from exc
        finally:
            await wp.close()

        alerts: list[Alert] = []
        wpscan_key = settings.WPSCAN_API_KEY
        semaphore = asyncio.Semaphore(5)

        # ── Phase 1: network lookups — concurrent, one pooled client ─────────
        async def fetch_plugin_data(client: httpx.AsyncClient, plugin: dict) -> dict | None:
            async with semaphore:
                slug = _extract_slug(plugin)
                installed_ver = plugin.get("version", "").strip()
                plugin_name = plugin.get("name", slug)

                if not slug or not installed_ver:
                    return None

                latest_ver, vulns = await asyncio.gather(
                    _fetch_wporg_version(client, slug),
                    _fetch_wpscan_vulns(client, slug, wpscan_key) if wpscan_key else asyncio.sleep(0),
                )

                latest_ver = latest_ver or installed_ver
                # None (lookup failed / no key) ≠ [] (confirmed no CVEs)
                vulns_unknown = not isinstance(vulns, list)

                # Filter vulns affecting installed version
                active_vulns = []
                for v in vulns if not vulns_unknown else []:
                    fixed_in = v.get("fixed_in")
                    # If no fixed_in, vuln is unpatched; if fixed_in > installed, affected
                    if not fixed_in or _version_lt(installed_ver, fixed_in):
                        active_vulns.append(v)

                return {
                    "slug": slug,
                    "installed_ver": installed_ver,
                    "plugin_name": plugin_name,
                    "latest_ver": latest_ver,
                    "active_vulns": active_vulns,
                    "vulns_unknown": vulns_unknown,
                }

        async with httpx.AsyncClient(
            timeout=10.0,
            limits=httpx.Limits(max_connections=5, max_keepalive_connections=5),
        ) as client:
            results = await asyncio.gather(*[fetch_plugin_data(client, p) for p in plugins])

        # Preload existing audit rows and plugin alerts once
        audits_r = await self.db.execute(
            select(PluginAudit).where(PluginAudit.site_id == site_id)
        )
        audits_by_slug = {a.plugin_slug: a for a in audits_r.scalars().all()}

        alerts_r = await self.db.execute(
            select(Alert).where(
                Alert.site_id == site_id,
                Alert.agent == "watchdog",
                Alert.type.in_(["plugin_outdated", "plugin_vulnerable"]),
            )
        )
        existing_alerts: dict[tuple[str, str], Alert] = {}
        stale_alert_ids: list[str] = []
        for a in alerts_r.scalars().all():
            key = ((a.metadata_ or {}).get("plugin_slug", ""), a.type)
            if key[0] and key not in existing_alerts:
                existing_alerts[key] = a
            else:
                stale_alert_ids.append(a.id)  # no slug or duplicate — drop

        # ── Phase 2: DB writes — sequential (AsyncSession is not concurrency-safe)
        seen_slugs: set[str] = set()
        for data in results:
            if not data:
                continue
            slug = data["slug"]
            installed_ver = data["installed_ver"]
            plugin_name = data["plugin_name"]
            latest_ver = data["latest_ver"]
            active_vulns = data["active_vulns"]
            seen_slugs.add(slug)

            # Outdated means installed < latest — a locally newer version
            # (rollback, beta) or a failed wp.org lookup is NOT outdated.
            is_outdated = _version_lt(installed_ver, latest_ver)
            has_vuln = len(active_vulns) > 0
            risk = "critical" if has_vuln else ("high" if is_outdated else "low")

            audit = audits_by_slug.get(slug)
            vuln_details = {"vulnerabilities": active_vulns} if active_vulns else {}
            if audit:
                audit.installed_version = installed_ver
                audit.latest_version = latest_ver
                audit.risk_level = risk
                audit.plugin_name = plugin_name
                audit.vulnerability_details = vuln_details
            else:
                audit = PluginAudit(
                    site_id=site_id,
                    plugin_slug=slug,
                    plugin_name=plugin_name,
                    installed_version=installed_ver,
                    latest_version=latest_ver,
                    risk_level=risk,
                    vulnerability_details=vuln_details,
                )
                self.db.add(audit)

            # Desired alert for this plugin: vulnerable > outdated > none
            if has_vuln:
                vuln_names = [v.get("title", "Unknown CVE") for v in active_vulns[:3]]
                desired = (
                    "plugin_vulnerable",
                    "critical",
                    f"Vulnerable plugin: {plugin_name} v{installed_ver}",
                    (
                        f"{len(active_vulns)} known vuln(s): {vuln_names[0]}. "
                        + (f"Update to v{latest_ver} to patch." if is_outdated else "No patch available yet.")
                    ),
                    {
                        "plugin_slug": slug,
                        "plugin_name": plugin_name,
                        "installed_version": installed_ver,
                        "latest_version": latest_ver,
                        "vulnerability_count": len(active_vulns),
                        "vulnerability": active_vulns[0] if active_vulns else {},
                    },
                )
            elif is_outdated:
                desired = (
                    "plugin_outdated",
                    "warning",
                    f"Plugin outdated: {plugin_name}",
                    (
                        f"v{installed_ver} installed, v{latest_ver} available. "
                        "Update for security patches and bug fixes."
                    ),
                    {
                        "plugin_slug": slug,
                        "plugin_name": plugin_name,
                        "installed_version": installed_ver,
                        "latest_version": latest_ver,
                    },
                )
            else:
                desired = None

            # WPScan unreachable + a CVE alert already exists: keep everything
            # as-is for this plugin — an outage must never clear a CVE alert.
            if data["vulns_unknown"] and (slug, "plugin_vulnerable") in existing_alerts:
                continue

            # Reconcile: update in place (dismissed status and created_at
            # survive), create only when new, drop the now-wrong alert type.
            for alert_type in ("plugin_vulnerable", "plugin_outdated"):
                current = existing_alerts.get((slug, alert_type))
                if desired and desired[0] == alert_type:
                    if current:
                        current.severity, current.title = desired[1], desired[2]
                        current.description, current.metadata_ = desired[3], desired[4]
                    else:
                        alert = await self.create_alert(
                            site_id=site_id,
                            agent="watchdog",
                            severity=desired[1],
                            type_=alert_type,
                            title=desired[2],
                            description=desired[3],
                            metadata=desired[4],
                        )
                        alerts.append(alert)
                elif current:
                    stale_alert_ids.append(current.id)

        # Plugins that were uninstalled: drop their audit rows and alerts
        for slug, audit in audits_by_slug.items():
            if slug not in seen_slugs:
                await self.db.delete(audit)
        stale_alert_ids += [
            a.id for (slug, _), a in existing_alerts.items() if slug not in seen_slugs
        ]
        if stale_alert_ids:
            await self.db.execute(delete(Alert).where(Alert.id.in_(set(stale_alert_ids))))

        await self.db.flush()
        return alerts


def _version_lt(v1: str, v2: str) -> bool:
    """Return True if v1 < v2 using simple numeric tuple comparison."""
    def parts(v: str) -> tuple[int, ...]:
        try:
            return tuple(int(x) for x in v.split(".") if x.isdigit())
        except Exception:
            return (0,)

    p1, p2 = parts(v1), parts(v2)
    # Pad to same length
    length = max(len(p1), len(p2))
    p1 += (0,) * (length - len(p1))
    p2 += (0,) * (length - len(p2))
    return p1 < p2
