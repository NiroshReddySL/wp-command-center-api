"""WPScan Vulnerability Database.

The only source that answers "is this exact version exploitable", as opposed
to WordPress.org's "is there a newer one". Those are different questions and
only the first is a security answer.

Everything here is shaped by one constraint: the free plan allows 25 requests
a day, and a single audit needs one per tracked component. So callers are
expected to cache results and spend the remaining allowance deliberately —
`fetch_quota` exists so that budget can be read from the API rather than
guessed at.
"""
import logging
from dataclasses import dataclass

import httpx

from app.connectors.retry import request_with_retries

logger = logging.getLogger(__name__)

API_BASE = "https://wpscan.com/api/v3"


@dataclass(frozen=True)
class VulnLookup:
    """`vulns is None` means the lookup failed — unknown, never "clean".

    The distinction is the whole point: an outage, a missing key and a
    rejected key must never be recorded as "no vulnerabilities found".
    """

    vulns: list[dict] | None
    auth_failed: bool = False


@dataclass(frozen=True)
class Quota:
    limit: int
    remaining: int


async def fetch_quota(client: httpx.AsyncClient, api_key: str) -> Quota | None:
    """Requests left today, or None if it can't be determined.

    WPScan exposes /status precisely so clients can pace themselves.
    """
    try:
        resp = await client.get(
            f"{API_BASE}/status",
            headers={"Authorization": f"Token token={api_key}"},
            timeout=10.0,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        return Quota(
            limit=int(data.get("requests_limit") or 0),
            remaining=int(data.get("requests_remaining") or 0),
        )
    except Exception as exc:
        logger.warning("WPScan quota check failed: %s", exc)
        return None


async def fetch_vulnerabilities(
    client: httpx.AsyncClient, slug: str, component_type: str, api_key: str
) -> VulnLookup:
    """Every known vulnerability for a component, each with its `fixed_in`.

    Deliberately NOT filtered to an installed version: the response is
    version-independent, which is what lets one fetch be cached and then
    reused across sites and across upgrades.
    """
    plural = "themes" if component_type == "theme" else "plugins"
    try:
        resp = await request_with_retries(
            lambda: client.get(
                f"{API_BASE}/{plural}/{slug}",
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
        if resp.status_code == 404:
            # Genuinely unknown to WPScan — a real "no data", not a failure.
            return VulnLookup([])
        if resp.status_code in (401, 403):
            # A rejected key fails identically for every component, forever,
            # and silently. Flagged so it can be reported once, loudly.
            logger.warning("WPScan rejected the API key (HTTP %d)", resp.status_code)
            return VulnLookup(None, auth_failed=True)
        if resp.status_code == 429:
            logger.warning("WPScan daily quota exhausted — %s left unchecked", slug)
            return VulnLookup(None)
        logger.warning(
            "WPScan returned HTTP %d for %s — vulnerability status unknown",
            resp.status_code, slug,
        )
    except Exception as exc:
        logger.warning("WPScan lookup failed for %s — status unknown: %s", slug, exc)
    return VulnLookup(None)


__all__ = ["API_BASE", "Quota", "VulnLookup", "fetch_quota", "fetch_vulnerabilities"]
