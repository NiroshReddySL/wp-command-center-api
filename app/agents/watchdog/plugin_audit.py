"""Component Audit — outdated versions and known CVEs for plugins and themes.

Two things this module refuses to do, because both would report a site as
safe when nobody actually checked it:

1. Return an empty list when the site has no Application Password. Reading
   `/wp/v2/plugins` and `/wp/v2/themes` requires authentication, so an
   unconnected site produced no findings at all — indistinguishable, on the
   Watchdog page, from a site with a clean bill of health. Components can now
   be entered by hand instead, and when there is genuinely nothing to audit
   the module says so out loud with an `info` alert.

2. Treat a failed vulnerability lookup as "no vulnerabilities". A WPScan
   outage, a missing key and a *rejected* key all mean unknown, never clean —
   and a rejected key is reported separately, because it fails identically
   forever and would otherwise never surface anywhere.
"""
import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Any

import httpx
from sqlalchemy import delete, select

from app.agents.base import BaseAgent
from app.config import settings
from app.connectors.retry import request_with_retries
from app.connectors.wordpress import WordPressConnector
from app.database.models import Alert, PluginAudit, Site

logger = logging.getLogger(__name__)

COMPONENT_PLUGIN = "plugin"
COMPONENT_THEME = "theme"
COMPONENT_TYPES = (COMPONENT_PLUGIN, COMPONENT_THEME)

SOURCE_WORDPRESS = "wordpress"
SOURCE_MANUAL = "manual"

# Where `latest_version` came from. The distinction matters because two of
# these three look identical in the data — latest == installed — while meaning
# opposite things: genuinely current, versus never looked up.
LATEST_WPORG = "wporg"
LATEST_MANUAL = "manual"
LATEST_UNKNOWN = "unknown"

# Site-level notices, not tied to any one component.
UNAVAILABLE_TYPE = "component_audit_unavailable"
WPSCAN_AUTH_TYPE = "component_audit_wpscan_auth"


@dataclass(frozen=True)
class Component:
    """One auditable thing, however it was discovered."""

    component_type: str
    slug: str
    name: str
    version: str
    is_active: bool | None
    source: str
    # Set for components the WordPress.org directory does not carry — premium
    # and custom builds — where the operator is the only possible authority on
    # what the newest release is.
    latest_override: str | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.component_type, self.slug)

    @property
    def label(self) -> str:
        return "Theme" if self.component_type == COMPONENT_THEME else "Plugin"


# ── Parsing WordPress payloads ───────────────────────────────────────────────

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


def _rendered(value: Any) -> str:
    """Themes return `name` as {"raw": ..., "rendered": ...}; plugins use a
    plain string. Accept both rather than guessing per endpoint."""
    if isinstance(value, dict):
        return str(value.get("rendered") or value.get("raw") or "").strip()
    return str(value or "").strip()


def _status_to_active(status: Any) -> bool | None:
    """WordPress reports "active"/"inactive". Anything else is unknown, which
    is a different thing from inactive and must not be flattened into it."""
    if not isinstance(status, str):
        return None
    lowered = status.lower()
    if lowered == "active":
        return True
    if lowered == "inactive":
        return False
    return None


def plugin_component(raw: dict) -> Component | None:
    slug = _extract_slug(raw)
    version = str(raw.get("version") or "").strip()
    if not slug or not version:
        return None
    return Component(
        component_type=COMPONENT_PLUGIN,
        slug=slug,
        name=_rendered(raw.get("name")) or slug,
        version=version,
        is_active=_status_to_active(raw.get("status")),
        source=SOURCE_WORDPRESS,
    )


def theme_component(raw: dict) -> Component | None:
    # `stylesheet` is the theme's own directory name — the slug wp.org and
    # WPScan key on. `template` is the PARENT of a child theme, so it names
    # the wrong thing for the component actually installed.
    slug = str(raw.get("stylesheet") or raw.get("template") or "").lower().strip()
    version = _rendered(raw.get("version"))
    if not slug or not version:
        return None
    return Component(
        component_type=COMPONENT_THEME,
        slug=slug,
        name=_rendered(raw.get("name")) or slug,
        version=version,
        is_active=_status_to_active(raw.get("status")),
        source=SOURCE_WORDPRESS,
    )


# ── Upstream lookups ─────────────────────────────────────────────────────────

async def _fetch_wporg_version(
    client: httpx.AsyncClient, slug: str, component_type: str
) -> str | None:
    """Latest published version from WordPress.org, or None on failure.

    The two APIs disagree about how to say "never heard of it": the plugin
    endpoint returns `{"error": ...}` while the theme endpoint returns the
    bare JSON literal `false`. Both have to read as unknown, never as a
    version, or every unlisted component would look outdated.
    """
    if component_type == COMPONENT_THEME:
        url = "https://api.wordpress.org/themes/info/1.1/"
        params: dict[str, str] = {"action": "theme_information", "request[slug]": slug}
    else:
        url = f"https://api.wordpress.org/plugins/info/1.0/{slug}.json"
        params = {}
    try:
        resp = await request_with_retries(
            lambda: client.get(url, params=params, timeout=8.0),
            what=f"wp.org {component_type} {slug}",
        )
        if resp.status_code == 200:
            data = resp.json()
            if isinstance(data, dict) and not data.get("error"):
                version = data.get("version")
                return str(version) if version else None
    except Exception as exc:
        logger.warning("wordpress.org %s lookup failed for %s: %s", component_type, slug, exc)
    return None


@dataclass(frozen=True)
class VulnLookup:
    """`vulns is None` means the lookup failed — unknown, never "clean"."""

    vulns: list[dict] | None
    auth_failed: bool = False


async def _fetch_wpscan_vulns(
    client: httpx.AsyncClient, slug: str, component_type: str, api_key: str
) -> VulnLookup:
    plural = "themes" if component_type == COMPONENT_THEME else "plugins"
    try:
        resp = await request_with_retries(
            lambda: client.get(
                f"https://wpscan.com/api/v3/{plural}/{slug}",
                headers={"Authorization": f"Token token={api_key}"},
                timeout=10.0,
            ),
            what=f"WPScan {component_type} {slug}",
        )
        if resp.status_code == 200:
            data = resp.json()
            body = data.get(slug, {}) if isinstance(data, dict) else {}
            vulns = body.get("vulnerabilities") if isinstance(body, dict) else None
            return VulnLookup(vulns if isinstance(vulns, list) else [])
        if resp.status_code == 404:  # unknown to WPScan — genuinely no data
            return VulnLookup([])
        if resp.status_code in (401, 403):
            logger.warning("WPScan rejected the API key (HTTP %d)", resp.status_code)
            return VulnLookup(None, auth_failed=True)
        logger.warning(
            "WPScan returned HTTP %d for %s — vulnerability status unknown",
            resp.status_code, slug,
        )
    except Exception as exc:
        logger.warning("WPScan lookup failed for %s — status unknown: %s", slug, exc)
    return VulnLookup(None)


# ── Version comparison ───────────────────────────────────────────────────────

_VERSION_SEGMENT = re.compile(r"(\d+)(.*)")


def _parse_version(v: str) -> tuple[tuple[int, ...], str]:
    """Split a version into (release numbers, prerelease suffix).

    "2.0.0-rc1" -> ((2, 0, 0), "rc1"). The suffix has to be kept rather than
    discarded: dropping it made "2.0.0-rc1" parse as (2, 0) and then pad back
    to (2, 0, 0) — identical to the stable 2.0.0, so a site running a release
    candidate reported as perfectly up to date.
    """
    release: list[int] = []
    suffix = ""
    for raw in (v or "").strip().lstrip("vV").split("."):
        match = _VERSION_SEGMENT.match(raw)
        if not match:
            suffix = suffix or raw
            break
        release.append(int(match.group(1)))
        rest = match.group(2)
        if rest:
            suffix = rest.lstrip("-_.+")
            break
    return tuple(release), suffix.lower()


def _version_lt(v1: str, v2: str) -> bool:
    """Return True if v1 is strictly older than v2."""
    r1, s1 = _parse_version(v1)
    r2, s2 = _parse_version(v2)
    length = max(len(r1), len(r2))
    r1 += (0,) * (length - len(r1))
    r2 += (0,) * (length - len(r2))
    if r1 != r2:
        return r1 < r2
    # Same release numbers: a prerelease sorts BELOW the final release.
    if bool(s1) != bool(s2):
        return bool(s1)
    return s1 < s2


def active_vulnerabilities(installed_version: str, vulns: list[dict]) -> list[dict]:
    """Vulnerabilities still affecting the installed version.

    No `fixed_in` means nobody has shipped a patch, so it applies whatever is
    installed — the most serious case, and exactly the one a naive
    "is there a newer release?" check misses.
    """
    out: list[dict] = []
    for v in vulns:
        fixed_in = v.get("fixed_in")
        if not fixed_in or _version_lt(installed_version, str(fixed_in)):
            out.append(v)
    return out


def _alert_type(component_type: str, *, vulnerable: bool) -> str:
    return f"{component_type}_{'vulnerable' if vulnerable else 'outdated'}"


ALERT_TYPES: tuple[str, ...] = tuple(
    _alert_type(ct, vulnerable=v) for ct in COMPONENT_TYPES for v in (True, False)
)
NOTICE_TYPES: tuple[str, ...] = (UNAVAILABLE_TYPE, WPSCAN_AUTH_TYPE)


class PluginAuditor(BaseAgent):
    async def run(self, site_id: str) -> list[Alert]:
        result = await self.db.execute(select(Site).where(Site.id == site_id))
        site = result.scalar_one_or_none()
        if not site:
            return []

        audits_r = await self.db.execute(
            select(PluginAudit).where(PluginAudit.site_id == site_id)
        )
        audits_by_key: dict[tuple[str, str], PluginAudit] = {
            (a.component_type, a.plugin_slug): a for a in audits_r.scalars().all()
        }

        components, wp_read = await self._collect(site, audits_by_key)
        existing_alerts, stale_alert_ids = await self._load_alerts(site_id)
        alerts: list[Alert] = []

        if not components:
            alerts += await self._notice(
                site_id, existing_alerts, UNAVAILABLE_TYPE, present=True,
                severity="info",
                title=f"Plugin & theme audit unavailable — {site.name}",
                description=(
                    "This site has no Application Password, so WordPress will not share "
                    "its plugin or theme list, and nothing has been recorded manually. "
                    "Connect a password, or add components by hand, to have them checked "
                    "for updates and known vulnerabilities."
                    if not site.api_key else
                    "WordPress returned no plugins or themes for this site."
                ),
                metadata={"reason": "no_api_key" if not site.api_key else "empty_inventory"},
            )
            # Nothing could be verified, so previous component findings are
            # left exactly as they are rather than cleared.
            await self._drop(stale_alert_ids)
            await self.db.flush()
            return alerts

        alerts += await self._notice(
            site_id, existing_alerts, UNAVAILABLE_TYPE, present=False,
            severity="info", title="", description="", metadata={},
        )

        enriched, wpscan_auth_failed = await self._enrich(components)

        alerts += await self._notice(
            site_id, existing_alerts, WPSCAN_AUTH_TYPE, present=wpscan_auth_failed,
            severity="warning",
            title="Vulnerability database rejected the WPScan API key",
            description=(
                "WPScan answered 401/403, so nothing could be checked against the "
                "vulnerability database. Version-update findings are still accurate; "
                "known-CVE status is simply unknown. Check WPSCAN_API_KEY."
            ),
            metadata={"reason": "wpscan_auth"},
        )

        seen: set[tuple[str, str]] = set()
        for component, latest_ver, vulns in enriched:
            seen.add(component.key)
            alerts += await self._apply(
                site_id, component, latest_ver, vulns, audits_by_key, existing_alerts
            )

        # Components WordPress no longer reports were uninstalled. Only
        # WordPress-sourced rows may be reaped this way: a manual entry is the
        # user's own record and disappears only when they delete it.
        if wp_read:
            for key, audit in list(audits_by_key.items()):
                if key in seen or audit.source != SOURCE_WORDPRESS:
                    continue
                await self.db.delete(audit)
                audits_by_key.pop(key, None)
                prefix = f"{key[0]}:{key[1]}"
                stale_alert_ids += [
                    a.id for (slug_key, _), a in existing_alerts.items() if slug_key == prefix
                ]

        await self._drop(stale_alert_ids)
        logger.info(
            "ComponentAudit %s: %d component(s) — %d from WordPress, %d manual",
            site.url, len(components),
            sum(1 for c in components if c.source == SOURCE_WORDPRESS),
            sum(1 for c in components if c.source == SOURCE_MANUAL),
        )
        await self.db.flush()
        return alerts

    # ── Collection ───────────────────────────────────────────────────────────

    async def _collect(
        self, site: Site, audits_by_key: dict[tuple[str, str], PluginAudit]
    ) -> tuple[list[Component], bool]:
        """Everything auditable for this site, and whether WordPress was read.

        WordPress wins over a manual entry for the same component: when the
        site is connected the live install is the truth, and a hand-typed
        version left over from before must not override it.
        """
        components: dict[tuple[str, str], Component] = {}
        wp_read = False

        if site.api_key:
            wp = WordPressConnector(site.url, site.api_key)
            try:
                raw_plugins, raw_themes = await asyncio.gather(
                    wp.get_plugins(), wp.get_themes()
                )
            except Exception as exc:
                # Silent [] would render as "all components healthy".
                raise RuntimeError(
                    f"Could not fetch components from {site.url}: {exc}"
                ) from exc
            finally:
                await wp.close()
            wp_read = True
            for raw, parse in ((raw_plugins, plugin_component), (raw_themes, theme_component)):
                for item in raw or []:
                    parsed = parse(item) if isinstance(item, dict) else None
                    if parsed:
                        components[parsed.key] = parsed

        for key, audit in audits_by_key.items():
            if audit.source != SOURCE_MANUAL or key in components:
                continue
            components[key] = Component(
                component_type=audit.component_type,
                slug=audit.plugin_slug,
                name=audit.plugin_name or audit.plugin_slug,
                version=audit.installed_version,
                is_active=audit.is_active,
                source=SOURCE_MANUAL,
                latest_override=(
                    audit.latest_version if audit.latest_source == LATEST_MANUAL else None
                ),
            )

        return list(components.values()), wp_read

    # ── Enrichment ───────────────────────────────────────────────────────────

    async def _enrich(
        self, components: list[Component]
    ) -> tuple[list[tuple[Component, str | None, list[dict] | None]], bool]:
        wpscan_key = settings.WPSCAN_API_KEY
        semaphore = asyncio.Semaphore(5)
        auth_failed = False

        async def lookup(
            client: httpx.AsyncClient, component: Component
        ) -> tuple[Component, str | None, list[dict] | None]:
            nonlocal auth_failed
            async with semaphore:
                if wpscan_key:
                    latest, vuln = await asyncio.gather(
                        _fetch_wporg_version(client, component.slug, component.component_type),
                        _fetch_wpscan_vulns(
                            client, component.slug, component.component_type, wpscan_key
                        ),
                    )
                else:
                    latest = await _fetch_wporg_version(
                        client, component.slug, component.component_type
                    )
                    vuln = VulnLookup(None)
                if vuln.auth_failed:
                    auth_failed = True
                # `latest` stays None on a failed lookup. Substituting the
                # installed version here is what made an unlisted component
                # compare equal and report as up to date.
                return component, latest, vuln.vulns

        async with httpx.AsyncClient(
            timeout=10.0,
            limits=httpx.Limits(max_connections=5, max_keepalive_connections=5),
        ) as client:
            results = await asyncio.gather(*[lookup(client, c) for c in components])

        return list(results), auth_failed

    # ── Alerts ───────────────────────────────────────────────────────────────

    async def _load_alerts(
        self, site_id: str
    ) -> tuple[dict[tuple[str, str], Alert], list[str]]:
        rows = await self.db.execute(
            select(Alert).where(
                Alert.site_id == site_id,
                Alert.agent == "watchdog",
                Alert.type.in_([*ALERT_TYPES, *NOTICE_TYPES]),
            )
        )
        existing: dict[tuple[str, str], Alert] = {}
        stale: list[str] = []
        for a in rows.scalars().all():
            meta = a.metadata_ or {}
            if a.type in NOTICE_TYPES:
                key = ("", a.type)
            else:
                slug = meta.get("plugin_slug", "")
                ctype = meta.get("component_type", COMPONENT_PLUGIN)
                if not slug:
                    stale.append(a.id)  # unkeyable legacy row
                    continue
                key = (f"{ctype}:{slug}", a.type)
            if key in existing:
                stale.append(a.id)  # duplicate
            else:
                existing[key] = a
        return existing, stale

    async def _drop(self, alert_ids: list[str]) -> None:
        if alert_ids:
            await self.db.execute(delete(Alert).where(Alert.id.in_(set(alert_ids))))

    @staticmethod
    def _resolve_latest(component: Component, wporg_version: str | None) -> tuple[str, str]:
        """(latest_version, latest_source).

        Order matters. The directory is authoritative when it has a record.
        Otherwise an operator-supplied version wins, because for a premium or
        custom component they are the only possible authority. Failing both,
        latest mirrors installed and is flagged `unknown` — the comparison is
        then a deliberate no-op rather than a claim that it is current.
        """
        if wporg_version:
            return wporg_version, LATEST_WPORG
        if component.latest_override:
            return component.latest_override, LATEST_MANUAL
        return component.version, LATEST_UNKNOWN

    async def _apply(
        self,
        site_id: str,
        component: Component,
        wporg_version: str | None,
        vulns: list[dict] | None,
        audits_by_key: dict[tuple[str, str], PluginAudit],
        existing_alerts: dict[tuple[str, str], Alert],
    ) -> list[Alert]:
        latest_ver, latest_source = self._resolve_latest(component, wporg_version)
        active_vulns = active_vulnerabilities(component.version, vulns) if vulns else []
        vulns_unknown = vulns is None
        is_outdated = _version_lt(component.version, latest_ver)
        has_vuln = bool(active_vulns)
        # "unknown" is not a clean result, so it does not earn "low".
        risk = (
            "critical" if has_vuln
            else "high" if is_outdated
            else "unknown" if latest_source == LATEST_UNKNOWN
            else "low"
        )

        audit = audits_by_key.get(component.key)
        vuln_details = {"vulnerabilities": active_vulns} if active_vulns else {}
        if audit:
            audit.installed_version = component.version
            audit.latest_version = latest_ver
            audit.latest_source = latest_source
            audit.risk_level = risk
            audit.plugin_name = component.name
            audit.vulnerability_details = vuln_details
            # WordPress is authoritative about its own installs; a manual row
            # keeps whatever the user recorded.
            if component.source == SOURCE_WORDPRESS:
                audit.is_active = component.is_active
                audit.source = SOURCE_WORDPRESS
        else:
            self.db.add(PluginAudit(
                site_id=site_id,
                plugin_slug=component.slug,
                plugin_name=component.name,
                component_type=component.component_type,
                installed_version=component.version,
                latest_version=latest_ver,
                latest_source=latest_source,
                risk_level=risk,
                vulnerability_details=vuln_details,
                is_active=component.is_active,
                source=component.source,
            ))

        state = "" if component.is_active is None else (
            " Currently active." if component.is_active else " Installed but not active."
        )
        origin = "" if component.source == SOURCE_WORDPRESS else " Recorded manually."

        vuln_type = _alert_type(component.component_type, vulnerable=True)
        outdated_type = _alert_type(component.component_type, vulnerable=False)

        if has_vuln:
            names = [v.get("title", "Unknown CVE") for v in active_vulns[:3]]
            desired: tuple[str, str, str, str, dict[str, Any]] | None = (
                vuln_type, "critical",
                f"Vulnerable {component.component_type}: {component.name} v{component.version}",
                (
                    f"{len(active_vulns)} known vuln(s): {names[0]}. "
                    + (f"Update to v{latest_ver} to patch." if is_outdated
                       else "No patch available yet.")
                    + state + origin
                ),
                self._meta(component, latest_ver, latest_source,
                           vuln_count=len(active_vulns), vulnerability=active_vulns[0]),
            )
        elif is_outdated:
            desired = (
                outdated_type, "warning",
                f"{component.label} outdated: {component.name}",
                (
                    f"v{component.version} installed, v{latest_ver} available. "
                    "Update for security patches and bug fixes." + state + origin
                ),
                self._meta(component, latest_ver, latest_source),
            )
        else:
            desired = None

        # WPScan unreachable and a CVE alert already exists: leave this
        # component alone. An outage must never clear a CVE alert.
        alert_key = f"{component.component_type}:{component.slug}"
        if vulns_unknown and (alert_key, vuln_type) in existing_alerts:
            return []

        created: list[Alert] = []
        for alert_type in (vuln_type, outdated_type):
            key = (alert_key, alert_type)
            current = existing_alerts.pop(key, None)
            if desired and desired[0] == alert_type:
                if current:
                    await self.update_alert(
                        current, severity=desired[1], title=desired[2],
                        description=desired[3], metadata=desired[4],
                    )
                else:
                    created.append(await self.create_alert(
                        site_id=site_id, agent="watchdog", severity=desired[1],
                        type_=alert_type, title=desired[2],
                        description=desired[3], metadata=desired[4],
                    ))
            elif current:
                await self.db.execute(delete(Alert).where(Alert.id == current.id))
        return created

    @staticmethod
    def _meta(
        component: Component, latest_ver: str, latest_source: str, *,
        vuln_count: int = 0, vulnerability: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        meta: dict[str, Any] = {
            "plugin_slug": component.slug,
            "plugin_name": component.name,
            "component_type": component.component_type,
            "installed_version": component.version,
            "latest_version": latest_ver,
            "latest_source": latest_source,
            "is_active": component.is_active,
            "source": component.source,
        }
        if vuln_count:
            meta["vulnerability_count"] = vuln_count
            meta["vulnerability"] = vulnerability or {}
        return meta

    async def _notice(
        self,
        site_id: str,
        existing_alerts: dict[tuple[str, str], Alert],
        alert_type: str,
        *,
        present: bool,
        severity: str,
        title: str,
        description: str,
        metadata: dict[str, Any],
    ) -> list[Alert]:
        """Create, refresh or clear a site-level notice (not tied to a component)."""
        current = existing_alerts.pop(("", alert_type), None)
        if not present:
            if current:
                await self.db.execute(delete(Alert).where(Alert.id == current.id))
            return []
        if current:
            await self.update_alert(
                current, severity=severity, title=title,
                description=description, metadata=metadata,
            )
            return []
        return [await self.create_alert(
            site_id=site_id, agent="watchdog", severity=severity,
            type_=alert_type, title=title, description=description, metadata=metadata,
        )]
