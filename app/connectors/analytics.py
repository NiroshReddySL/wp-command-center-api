"""Google Analytics 4 Data API connector."""
import re
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from app.config import settings

_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GA4_URL = "https://analyticsdata.googleapis.com/v1beta"
# Funnel Reports live only under the v1alpha surface — the only part of the
# GA4 Data API that can evaluate an ORDERED sequence of steps server-side
# without needing BigQuery Export (which is the only place GA4 exposes raw
# per-session event order). Same OAuth scope as v1beta, just a newer/less
# stable API stage.
_GA4_URL_ALPHA = "https://analyticsdata.googleapis.com/v1alpha"

# Flow-category step match types -> GA4 StringFilter matchType.
_FUNNEL_MATCH_TYPES = {"contains": "CONTAINS", "exact": "EXACT", "regex": "FULL_REGEXP"}

# ISO 3166-1 alpha-2 → macro-region mapping
_REGION_LOOKUP: dict[str, str] = {
    # North America
    **{c: "North America" for c in ["US", "CA", "MX"]},
    # Latin America
    **{c: "Latin America" for c in [
        "BR", "AR", "CL", "CO", "PE", "VE", "EC", "BO", "PY", "UY",
        "GT", "HN", "SV", "NI", "CR", "PA", "CU", "DO", "HT", "JM",
    ]},
    # Europe
    **{c: "Europe" for c in [
        "GB", "DE", "FR", "IT", "ES", "NL", "BE", "SE", "NO", "DK",
        "FI", "PL", "CZ", "AT", "CH", "PT", "GR", "HU", "RO", "BG",
        "HR", "SK", "SI", "LT", "LV", "EE", "IE", "LU", "MT", "CY",
        "UA", "RS", "BA", "AL", "MK", "ME", "XK", "MD", "BY", "IS",
    ]},
    # Middle East
    **{c: "Middle East" for c in [
        "SA", "AE", "QA", "KW", "BH", "OM", "JO", "LB", "IL", "IQ",
        "IR", "SY", "YE", "PS",
    ]},
    # Africa
    **{c: "Africa" for c in [
        "ZA", "NG", "KE", "ET", "GH", "TZ", "UG", "MA", "EG", "DZ",
        "TN", "SN", "CM", "CI", "MZ", "ZM", "ZW", "RW", "MG", "AO",
    ]},
    # South Asia
    **{c: "South Asia" for c in ["IN", "PK", "BD", "LK", "NP", "MV", "BT", "AF"]},
    # Southeast Asia
    **{c: "Southeast Asia" for c in [
        "SG", "MY", "TH", "PH", "ID", "VN", "MM", "KH", "LA", "BN",
    ]},
    # East Asia
    **{c: "East Asia" for c in ["CN", "JP", "KR", "TW", "HK", "MO", "MN"]},
    # Oceania
    **{c: "Oceania" for c in ["AU", "NZ", "PG", "FJ", "SB", "VU", "WS", "TO"]},
}


def _macro_region(country_code: str) -> str:
    return _REGION_LOOKUP.get(country_code.upper(), "Other")


async def _refresh_access_token(refresh_token: str) -> tuple[str, datetime]:
    """Exchange refresh token for a new access token. Returns (access_token, expiry)."""
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            _TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": settings.GA_CLIENT_ID,
                "client_secret": settings.GA_CLIENT_SECRET,
                "refresh_token": refresh_token,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        access_token: str = data["access_token"]
        expires_in: int = data.get("expires_in", 3600)
        expiry = datetime.now(timezone.utc) + timedelta(seconds=expires_in - 60)
        return access_token, expiry


def _build_funnel_step(step: dict[str, Any]) -> dict[str, Any]:
    """One flow-category step -> one GA4 FunnelStep. Every step (including
    the first) is a page-pattern condition matched against the `page_view`
    event's `page_location` parameter — GA4 has no separate "page_path"
    event parameter; page_location is the full URL, which is why "contains"
    is the recommended match type (robust to query strings and the
    domain/protocol prefix that an "exact" path could never match)."""
    ga_match_type = _FUNNEL_MATCH_TYPES.get(step["match_type"], "CONTAINS")
    funnel_step: dict[str, Any] = {
        "name": step["label"],
        "filterExpression": {
            "funnelEventFilter": {
                "eventName": "page_view",
                "funnelParameterFilterExpression": {
                    "funnelParameterFilter": {
                        "eventParameterName": "page_location",
                        "stringFilter": {
                            "value": step["pattern"],
                            "matchType": ga_match_type,
                            "caseSensitive": False,
                        },
                    }
                },
            }
        },
    }
    if step.get("is_directly_followed"):
        funnel_step["isDirectlyFollowedBy"] = True
    if step.get("within_seconds"):
        funnel_step["withinDurationFromPriorStep"] = f"{step['within_seconds']}s"
    return funnel_step


def _parse_step_index(step_name: str) -> int:
    """GA4 prefixes every funnel-table row's step name with "N. " (1-indexed)
    — parsed back to a reliable 0-indexed step number rather than matching
    on the marketer-supplied (possibly duplicate) label text."""
    match = re.match(r"^(\d+)\.", step_name)
    return int(match.group(1)) - 1 if match else 0


class AnalyticsConnector:
    def __init__(self, access_token: str) -> None:
        self.access_token = access_token

    @classmethod
    async def from_refresh_token(cls, refresh_token: str) -> "AnalyticsConnector":
        access_token, _ = await _refresh_access_token(refresh_token)
        return cls(access_token)

    async def get_daily_page_views(self, property_id: str, days: int = 30) -> list[dict[str, Any]]:
        """
        Fetch daily page views for the last N days.
        Returns list of { date: "MMM DD", views: int }.
        property_id should be in format "properties/123456789"
        """
        if not property_id.startswith("properties/"):
            property_id = f"properties/{property_id}"

        body = {
            "dateRanges": [{"startDate": f"{days}daysAgo", "endDate": "today"}],
            "dimensions": [{"name": "date"}],
            "metrics": [{"name": "screenPageViews"}],
            "orderBys": [{"dimension": {"dimensionName": "date"}}],
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{_GA4_URL}/{property_id}:runReport",
                headers={"Authorization": f"Bearer {self.access_token}"},
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()

        rows = data.get("rows", [])
        result = []
        for row in rows:
            raw_date = row["dimensionValues"][0]["value"]  # "20240115"
            dt = datetime.strptime(raw_date, "%Y%m%d")
            views = int(row["metricValues"][0]["value"])
            result.append({"date": dt.strftime("%b %d"), "sort_key": raw_date, "views": views})

        return result

    async def get_top_pages(self, property_id: str, days: int = 30, limit: int = 10) -> list[dict[str, Any]]:
        """Fetch top pages by page views."""
        if not property_id.startswith("properties/"):
            property_id = f"properties/{property_id}"

        body = {
            "dateRanges": [{"startDate": f"{days}daysAgo", "endDate": "today"}],
            "dimensions": [{"name": "pagePath"}, {"name": "pageTitle"}],
            "metrics": [{"name": "screenPageViews"}],
            "orderBys": [{"metric": {"metricName": "screenPageViews"}, "desc": True}],
            "limit": limit,
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{_GA4_URL}/{property_id}:runReport",
                headers={"Authorization": f"Bearer {self.access_token}"},
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()

        return [
            {
                "path": row["dimensionValues"][0]["value"],
                "title": row["dimensionValues"][1]["value"],
                "views": int(row["metricValues"][0]["value"]),
            }
            for row in data.get("rows", [])
        ]

    async def get_site_metrics(self, property_id: str, days: int = 1) -> dict[str, Any]:
        """Fetch aggregate site metrics: pageviews, sessions, users, bounce_rate, avg_session_duration."""
        if not property_id.startswith("properties/"):
            property_id = f"properties/{property_id}"

        body = {
            "dateRanges": [{"startDate": f"{days}daysAgo", "endDate": "yesterday"}],
            "metrics": [
                {"name": "screenPageViews"},
                {"name": "sessions"},
                {"name": "totalUsers"},
                {"name": "bounceRate"},
                {"name": "averageSessionDuration"},
            ],
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{_GA4_URL}/{property_id}:runReport",
                headers={"Authorization": f"Bearer {self.access_token}"},
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()

        rows = data.get("rows", [])
        if not rows:
            return {"pageviews": 0, "sessions": 0, "users": 0, "bounce_rate": 0.0, "avg_session_duration": 0.0}

        vals = rows[0]["metricValues"]
        return {
            "pageviews": int(vals[0]["value"]),
            "sessions": int(vals[1]["value"]),
            "users": int(vals[2]["value"]),
            "bounce_rate": round(float(vals[3]["value"]) * 100, 1),
            "avg_session_duration": round(float(vals[4]["value"]), 1),
        }

    async def get_daily_site_metrics(self, property_id: str, days: int = 90) -> list[dict[str, Any]]:
        """Day-by-day sitewide metrics for the last `days` days, in ONE
        GA4 call — the historical backfill this powers doesn't need to wait
        for the nightly agent to accumulate history one calendar day at a
        time; GA4 already has it. Returns
        [{date: "YYYY-MM-DD", pageviews, sessions, users, bounce_rate, avg_session_duration}, ...],
        oldest first. GA4 omits a day's row entirely when nothing happened
        that day — callers should not assume every calendar date appears.
        """
        if not property_id.startswith("properties/"):
            property_id = f"properties/{property_id}"

        body = {
            "dateRanges": [{"startDate": f"{days}daysAgo", "endDate": "yesterday"}],
            "dimensions": [{"name": "date"}],
            "metrics": [
                {"name": "screenPageViews"}, {"name": "sessions"}, {"name": "totalUsers"},
                {"name": "bounceRate"}, {"name": "averageSessionDuration"},
            ],
            "orderBys": [{"dimension": {"dimensionName": "date"}}],
        }

        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                f"{_GA4_URL}/{property_id}:runReport",
                headers={"Authorization": f"Bearer {self.access_token}"},
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()

        result = []
        for row in data.get("rows", []):
            raw_date = row["dimensionValues"][0]["value"]  # "20260415"
            vals = row["metricValues"]
            result.append({
                "date": datetime.strptime(raw_date, "%Y%m%d").strftime("%Y-%m-%d"),
                "pageviews": int(vals[0]["value"]),
                "sessions": int(vals[1]["value"]),
                "users": int(vals[2]["value"]),
                "bounce_rate": round(float(vals[3]["value"]) * 100, 1),
                "avg_session_duration": round(float(vals[4]["value"]), 1),
            })
        return result

    async def get_geo_breakdown(self, property_id: str, days: int = 1) -> dict[str, Any]:
        """Fetch country, macro-region, and city breakdowns from GA4."""
        if not property_id.startswith("properties/"):
            property_id = f"properties/{property_id}"

        date_range = [{"startDate": f"{days}daysAgo", "endDate": "yesterday"}]

        async with httpx.AsyncClient(timeout=20.0) as client:
            headers = {"Authorization": f"Bearer {self.access_token}"}

            # Country breakdown
            country_resp = await client.post(
                f"{_GA4_URL}/{property_id}:runReport",
                headers=headers,
                json={
                    "dateRanges": date_range,
                    "dimensions": [{"name": "country"}, {"name": "countryId"}],
                    "metrics": [{"name": "screenPageViews"}, {"name": "sessions"}],
                    "orderBys": [{"metric": {"metricName": "screenPageViews"}, "desc": True}],
                    "limit": 30,
                },
            )
            country_resp.raise_for_status()

            # City breakdown
            city_resp = await client.post(
                f"{_GA4_URL}/{property_id}:runReport",
                headers=headers,
                json={
                    "dateRanges": date_range,
                    "dimensions": [{"name": "city"}, {"name": "country"}],
                    "metrics": [{"name": "screenPageViews"}],
                    "orderBys": [{"metric": {"metricName": "screenPageViews"}, "desc": True}],
                    "limit": 20,
                },
            )
            city_resp.raise_for_status()

        # Parse countries
        country_rows = country_resp.json().get("rows", [])
        countries = []
        total_views = sum(int(r["metricValues"][0]["value"]) for r in country_rows) or 1
        for row in country_rows:
            views = int(row["metricValues"][0]["value"])
            countries.append({
                "country": row["dimensionValues"][0]["value"],
                "country_code": row["dimensionValues"][1]["value"],
                "views": views,
                "sessions": int(row["metricValues"][1]["value"]),
                "pct": round(views / total_views * 100, 1),
            })

        # Aggregate into macro-regions
        region_map: dict[str, int] = {}
        for c in countries:
            region = _macro_region(c["country_code"])
            region_map[region] = region_map.get(region, 0) + c["views"]
        total_r = sum(region_map.values()) or 1
        regions = sorted(
            [{"region": k, "views": v, "pct": round(v / total_r * 100, 1)} for k, v in region_map.items()],
            key=lambda x: x["views"], reverse=True
        )

        # Parse cities
        city_rows = city_resp.json().get("rows", [])
        cities = [
            {
                "city": row["dimensionValues"][0]["value"],
                "country": row["dimensionValues"][1]["value"],
                "views": int(row["metricValues"][0]["value"]),
            }
            for row in city_rows
            if row["dimensionValues"][0]["value"] not in ("(not set)", "")
        ]

        return {"countries": countries, "regions": regions, "cities": cities}

    async def get_post_traffic(self, property_id: str, url_path: str, days: int = 30) -> int:
        """Get total page views for a specific URL path."""
        if not property_id.startswith("properties/"):
            property_id = f"properties/{property_id}"

        body = {
            "dateRanges": [{"startDate": f"{days}daysAgo", "endDate": "today"}],
            "dimensions": [{"name": "pagePath"}],
            "metrics": [{"name": "screenPageViews"}],
            "dimensionFilter": {
                "filter": {
                    "fieldName": "pagePath",
                    "stringFilter": {"value": url_path, "matchType": "EXACT"},
                }
            },
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{_GA4_URL}/{property_id}:runReport",
                headers={"Authorization": f"Bearer {self.access_token}"},
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()

        rows = data.get("rows", [])
        return int(rows[0]["metricValues"][0]["value"]) if rows else 0

    async def get_realtime_active_users_by_title(self, property_id: str) -> dict[str, int]:
        """Active users right now, grouped by page title.

        GA4's Realtime Reporting API — a genuinely different endpoint from
        the standard reports above — has NO page-path dimension, only
        `unifiedScreenName` (page title for web, screen name for apps).
        Matching a watched URL to this data is therefore done by title, not
        path; callers resolve each watched URL's title separately and look
        it up in the dict this returns. Rows sharing a title are summed.
        """
        if not property_id.startswith("properties/"):
            property_id = f"properties/{property_id}"

        body = {
            "dimensions": [{"name": "unifiedScreenName"}],
            "metrics": [{"name": "activeUsers"}],
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{_GA4_URL}/{property_id}:runRealtimeReport",
                headers={"Authorization": f"Bearer {self.access_token}"},
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()

        counts: dict[str, int] = {}
        for row in data.get("rows", []):
            title = row["dimensionValues"][0]["value"]
            users = int(row["metricValues"][0]["value"])
            counts[title] = counts.get(title, 0) + users
        return counts

    async def get_active_users_by_path(
        self, property_id: str, paths: list[str], start_date: str, end_date: str,
    ) -> dict[str, int]:
        """Active Users for a GA4-style date range (Today, Last 7 days, a
        custom range, ...), scoped to specific page paths.

        Unlike Realtime, the standard reporting API DOES have a `pagePath`
        dimension, so this matches directly on path — no title lookup
        needed. `paths` is passed through `inListFilter` so the query stays
        cheap regardless of how many pages the site has in total; rows
        sharing a path (e.g. differing by an implicit secondary dimension)
        are summed.
        """
        if not property_id.startswith("properties/"):
            property_id = f"properties/{property_id}"
        if not paths:
            return {}

        body = {
            "dateRanges": [{"startDate": start_date, "endDate": end_date}],
            "dimensions": [{"name": "pagePath"}],
            "metrics": [{"name": "activeUsers"}],
            "dimensionFilter": {
                "filter": {"fieldName": "pagePath", "inListFilter": {"values": paths}}
            },
            "limit": max(len(paths), 1),
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{_GA4_URL}/{property_id}:runReport",
                headers={"Authorization": f"Bearer {self.access_token}"},
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()

        counts: dict[str, int] = {}
        for row in data.get("rows", []):
            path = row["dimensionValues"][0]["value"]
            users = int(row["metricValues"][0]["value"])
            counts[path] = counts.get(path, 0) + users
        return counts

    async def get_daily_active_users_by_path(
        self, property_id: str, paths: list[str], start_date: str, end_date: str,
    ) -> dict[str, dict[str, int]]:
        """Day-by-day Active Users per page path over a date range — for a
        "day-wise breakdown" export, as opposed to get_active_users_by_path's
        single range total. Adds `date` as a second dimension alongside
        `pagePath`; GA4 returns it as "YYYYMMDD", reformatted here to
        YYYY-MM-DD to match every other date this module hands back.

        Returns {path: {iso_date: active_users}}; a day with zero activity
        for a path simply has no entry (the caller fills gaps as 0).
        """
        if not property_id.startswith("properties/"):
            property_id = f"properties/{property_id}"
        if not paths:
            return {}

        body = {
            "dateRanges": [{"startDate": start_date, "endDate": end_date}],
            "dimensions": [{"name": "pagePath"}, {"name": "date"}],
            "metrics": [{"name": "activeUsers"}],
            "dimensionFilter": {
                "filter": {"fieldName": "pagePath", "inListFilter": {"values": paths}}
            },
            "limit": max(len(paths) * 400, 1),  # generous — up to ~400 days per path
        }

        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                f"{_GA4_URL}/{property_id}:runReport",
                headers={"Authorization": f"Bearer {self.access_token}"},
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()

        result: dict[str, dict[str, int]] = {}
        for row in data.get("rows", []):
            path = row["dimensionValues"][0]["value"]
            raw_date = row["dimensionValues"][1]["value"]  # "20260715"
            iso_date = datetime.strptime(raw_date, "%Y%m%d").strftime("%Y-%m-%d")
            users = int(row["metricValues"][0]["value"])
            bucket = result.setdefault(path, {})
            bucket[iso_date] = bucket.get(iso_date, 0) + users
        return result

    async def get_device_breakdown_by_path(
        self, property_id: str, paths: list[str], start_date: str, end_date: str,
    ) -> dict[str, int]:
        """Active users per device category ("desktop" | "mobile" | "tablet")
        for the given page paths over a date range.

        Returns {device: active_users}, summed across the supplied paths. GA4
        omits a category entirely when nobody used it, so an absent key means
        zero rather than missing data.
        """
        if not property_id.startswith("properties/"):
            property_id = f"properties/{property_id}"
        if not paths:
            return {}

        body = {
            "dateRanges": [{"startDate": start_date, "endDate": end_date}],
            "dimensions": [{"name": "deviceCategory"}],
            "metrics": [{"name": "activeUsers"}],
            "dimensionFilter": {
                "filter": {"fieldName": "pagePath", "inListFilter": {"values": paths}}
            },
            "limit": 20,
        }

        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                f"{_GA4_URL}/{property_id}:runReport",
                headers={"Authorization": f"Bearer {self.access_token}"},
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()

        result: dict[str, int] = {}
        for row in data.get("rows", []):
            device = row["dimensionValues"][0]["value"]
            users = int(row["metricValues"][0]["value"])
            result[device] = result.get(device, 0) + users
        return result

    async def get_engagement_metrics_by_path(
        self, property_id: str, paths: list[str], start_date: str, end_date: str,
    ) -> dict[str, dict[str, float]]:
        """Average engagement time per active user (seconds) and bounce rate
        (0-1) for a GA4-style date range, scoped to specific page paths.

        "Average engagement time per active user" isn't itself a queryable
        GA4 metric — GA4's own reports derive it as
        userEngagementDuration / activeUsers, so both raw metrics are
        fetched and divided here. Bounce rate is already an average (of
        engaged vs. total sessions), so rows sharing a path are merged by
        weighting each row's bounce rate by its sessions, not by naively
        averaging the ratios.
        """
        if not property_id.startswith("properties/"):
            property_id = f"properties/{property_id}"
        if not paths:
            return {}

        body = {
            "dateRanges": [{"startDate": start_date, "endDate": end_date}],
            "dimensions": [{"name": "pagePath"}],
            "metrics": [
                {"name": "userEngagementDuration"},
                {"name": "activeUsers"},
                {"name": "bounceRate"},
                {"name": "sessions"},
            ],
            "dimensionFilter": {
                "filter": {"fieldName": "pagePath", "inListFilter": {"values": paths}}
            },
            "limit": max(len(paths), 1),
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{_GA4_URL}/{property_id}:runReport",
                headers={"Authorization": f"Bearer {self.access_token}"},
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()

        totals: dict[str, dict[str, float]] = {}
        for row in data.get("rows", []):
            path = row["dimensionValues"][0]["value"]
            vals = row["metricValues"]
            engagement_duration = float(vals[0]["value"])
            active_users = float(vals[1]["value"])
            bounce_rate = float(vals[2]["value"])
            sessions = float(vals[3]["value"])

            bucket = totals.setdefault(path, {
                "engagement_duration": 0.0, "active_users": 0.0,
                "bounce_weighted": 0.0, "sessions": 0.0,
            })
            bucket["engagement_duration"] += engagement_duration
            bucket["active_users"] += active_users
            bucket["bounce_weighted"] += bounce_rate * sessions
            bucket["sessions"] += sessions

        result: dict[str, dict[str, float]] = {}
        for path, t in totals.items():
            avg_engagement_time = t["engagement_duration"] / t["active_users"] if t["active_users"] else 0.0
            merged_bounce_rate = t["bounce_weighted"] / t["sessions"] if t["sessions"] else 0.0
            result[path] = {"avg_engagement_time": avg_engagement_time, "bounce_rate": merged_bounce_rate}
        return result

    async def run_funnel_report(
        self, property_id: str, steps: list[dict[str, Any]], start_date: str, end_date: str,
        breakdown_dimension: str | None = None,
    ) -> dict[str, Any]:
        """Classify GA4 activity into a marketer-defined ordered flow via
        the Funnel Reports API (v1alpha) — closed funnel (`isOpenFunnel:
        false`), so a later step only counts if every prior step in
        `steps` was actually completed first, matching "ordered set of
        page-pattern steps".

        This is aggregate and USER-scoped, not session-scoped — GA4's
        standard Data API has no session-identifying dimension at all,
        and true per-session path reconstruction is only possible via
        BigQuery Export. `breakdown_dimension` (e.g. "deviceCategory",
        "sessionDefaultChannelGroup") is the closest honest equivalent to
        drill-down GA4 offers here: one extra dimension sliced across every
        step, never a list of individual sessions/users.

        Returns {
            "step_results": [{step_index, label, active_users,
                completion_rate, abandonments, abandonment_rate}, ...],
            "total_entered": int, "total_completed": int,
            "conversion_rate": float,           # 0-1
            "breakdown": [{value, step_index, active_users}, ...],
        }
        """
        if not property_id.startswith("properties/"):
            property_id = f"properties/{property_id}"
        if not steps:
            return {
                "step_results": [], "total_entered": 0, "total_completed": 0,
                "conversion_rate": 0.0, "breakdown": [],
            }

        body: dict[str, Any] = {
            "dateRanges": [{"startDate": start_date, "endDate": end_date}],
            "funnel": {"isOpenFunnel": False, "steps": [_build_funnel_step(s) for s in steps]},
        }
        if breakdown_dimension:
            body["funnelBreakdown"] = {"breakdownDimension": {"name": breakdown_dimension}, "limit": 10}

        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"{_GA4_URL_ALPHA}/{property_id}:runFunnelReport",
                headers={"Authorization": f"Bearer {self.access_token}"},
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()

        by_step_index: dict[int, dict[str, Any]] = {}
        breakdown: list[dict[str, Any]] = []

        for row in data.get("funnelTable", {}).get("rows", []):
            dim_values = row.get("dimensionValues", [])
            if not dim_values:
                continue
            step_idx = _parse_step_index(dim_values[0]["value"])
            metric_values = row.get("metricValues", [])
            active_users = int(float(metric_values[0]["value"])) if len(metric_values) > 0 else 0
            completion_rate = float(metric_values[1]["value"]) if len(metric_values) > 1 else 0.0
            abandonments = int(float(metric_values[2]["value"])) if len(metric_values) > 2 else 0
            abandonment_rate = float(metric_values[3]["value"]) if len(metric_values) > 3 else 0.0

            # No breakdown requested -> no second dimensionValue at all.
            # Breakdown requested -> "RESERVED_TOTAL" is the same all-
            # segments total as the no-breakdown case; any other value is
            # one real segment.
            breakdown_value = dim_values[1]["value"] if len(dim_values) > 1 else None
            if breakdown_value is None or breakdown_value == "RESERVED_TOTAL":
                by_step_index[step_idx] = {
                    "active_users": active_users, "completion_rate": completion_rate,
                    "abandonments": abandonments, "abandonment_rate": abandonment_rate,
                }
            else:
                breakdown.append({"value": breakdown_value, "step_index": step_idx, "active_users": active_users})

        # GA4 OMITS a step's row entirely once its active_users hits 0,
        # rather than returning a zero-value row — a single-day query with
        # no one completing the last step returns rows for every step
        # EXCEPT that one. Relying on "the last row GA4 sent back" would
        # then silently treat an earlier, non-zero step as if it were the
        # final one (turning a real 0% completion into a fabricated 100%).
        # Reconciling against the full requested step list is what makes a
        # missing row correctly read as "0 users reached this step".
        step_results: list[dict[str, Any]] = [
            {
                "step_index": i, "label": step["label"],
                **by_step_index.get(i, {
                    "active_users": 0, "completion_rate": 0.0, "abandonments": 0, "abandonment_rate": 0.0,
                }),
            }
            for i, step in enumerate(steps)
        ]
        total_entered = step_results[0]["active_users"]
        total_completed = step_results[-1]["active_users"]
        conversion_rate = (total_completed / total_entered) if total_entered else 0.0

        return {
            "step_results": step_results,
            "total_entered": total_entered,
            "total_completed": total_completed,
            "conversion_rate": conversion_rate,
            "breakdown": breakdown,
        }
