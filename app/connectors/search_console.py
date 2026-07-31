"""Google Search Console API connector."""
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

from app.config import settings

_TOKEN_URL = "https://oauth2.googleapis.com/token"
_GSC_BASE = "https://www.googleapis.com/webmasters/v3"


async def _refresh_access_token(refresh_token: str) -> str:
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            _TOKEN_URL,
            data={
                "grant_type": "refresh_token",
                "client_id": settings.GSC_CLIENT_ID,
                "client_secret": settings.GSC_CLIENT_SECRET,
                "refresh_token": refresh_token,
            },
        )
        resp.raise_for_status()
        return resp.json()["access_token"]


class SearchConsoleConnector:
    def __init__(self, access_token: str) -> None:
        self.access_token = access_token

    @classmethod
    async def from_refresh_token(cls, refresh_token: str) -> "SearchConsoleConnector":
        token = await _refresh_access_token(refresh_token)
        return cls(token)

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.access_token}"}

    async def get_top_queries(
        self, site_url: str, days: int = 90, limit: int = 100
    ) -> list[dict[str, Any]]:
        """
        Fetch top queries with clicks, impressions, CTR, and avg position.
        Returns list of { query, clicks, impressions, ctr, position }.
        """
        end = datetime.now(UTC).date()
        start = end - timedelta(days=days)

        body = {
            "startDate": str(start),
            "endDate": str(end),
            "dimensions": ["query"],
            "rowLimit": limit,
            "orderBy": [{"fieldName": "impressions", "sortOrder": "DESCENDING"}],
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{_GSC_BASE}/sites/{_encode_site(site_url)}/searchAnalytics/query",
                headers=self._headers(),
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()

        return [
            {
                "query": row["keys"][0],
                "clicks": row.get("clicks", 0),
                "impressions": row.get("impressions", 0),
                "ctr": round(row.get("ctr", 0) * 100, 1),
                "position": round(row.get("position", 0), 1),
            }
            for row in data.get("rows", [])
        ]

    async def get_daily_search_metrics(
        self, site_url: str, days: int = 90
    ) -> list[dict[str, Any]]:
        """Day-by-day organic search performance, in ONE call.

        Returns [{date: "YYYY-MM-DD", clicks, impressions, ctr, position}, ...]
        oldest first. This is the leading indicator GA4 alone can't give:
        impressions move BEFORE clicks do, so a ranking slide or a demand
        shift is visible days before it shows up as lost pageviews — and
        splitting traffic into impressions x CTR makes a drop attributable
        (lost rankings/demand vs. a SERP or snippet change) instead of just
        visible.

        GSC finalizes data ~2-3 days late, so the most recent days are
        simply absent from the response rather than zero — callers must not
        read a missing day as "no traffic".
        """
        end = datetime.now(UTC).date()
        start = end - timedelta(days=days)

        body = {
            "startDate": str(start),
            "endDate": str(end),
            "dimensions": ["date"],
            # One row per day; comfortably above any `days` we'd request.
            "rowLimit": 500,
        }

        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                f"{_GSC_BASE}/sites/{_encode_site(site_url)}/searchAnalytics/query",
                headers=self._headers(),
                json=body,
            )
            resp.raise_for_status()
            data = resp.json()

        return sorted(
            (
                {
                    # GSC already returns ISO dates here — no reformatting
                    # needed (unlike GA4's "20260715" form).
                    "date": row["keys"][0],
                    "clicks": int(row.get("clicks", 0)),
                    "impressions": int(row.get("impressions", 0)),
                    "ctr": round(row.get("ctr", 0) * 100, 2),
                    "position": round(row.get("position", 0), 1),
                }
                for row in data.get("rows", [])
            ),
            key=lambda r: r["date"],
        )

    def _page_filter(self, page_url: str) -> list[dict[str, Any]]:
        return [{"filters": [{"dimension": "page", "operator": "equals", "expression": page_url}]}]

    async def _page_query(
        self, site_url: str, page_url: str, start: str, end: str,
        dimensions: list[str], row_limit: int = 1,
    ) -> list[dict[str, Any]]:
        body = {
            "startDate": start,
            "endDate": end,
            "dimensions": dimensions,
            "dimensionFilterGroups": self._page_filter(page_url),
            "rowLimit": row_limit,
        }
        async with httpx.AsyncClient(timeout=25.0) as client:
            resp = await client.post(
                f"{_GSC_BASE}/sites/{_encode_site(site_url)}/searchAnalytics/query",
                headers=self._headers(),
                json=body,
            )
            if resp.status_code == 404:
                return []
            resp.raise_for_status()
            return resp.json().get("rows", [])

    async def get_page_search_summary(
        self, site_url: str, page_url: str, start: str, end: str,
    ) -> dict[str, float]:
        """Totals for ONE page over an explicit date window: clicks,
        impressions, ctr (%) and average position.

        Asked of GSC with no dimensions so Google computes the aggregates
        itself — in particular `position`, which is impression-weighted and
        would be wrong if naively averaged from daily or per-query rows.
        """
        rows = await self._page_query(site_url, page_url, start, end, dimensions=[])
        if not rows:
            return {"clicks": 0, "impressions": 0, "ctr": 0.0, "position": 0.0}
        m = rows[0]
        return {
            "clicks": int(m.get("clicks", 0)),
            "impressions": int(m.get("impressions", 0)),
            "ctr": round(m.get("ctr", 0) * 100, 2),
            "position": round(m.get("position", 0), 1),
        }

    async def get_page_daily_search(
        self, site_url: str, page_url: str, start: str, end: str,
    ) -> list[dict[str, Any]]:
        """Day-by-day clicks/impressions for one page, oldest first. Days
        with no search activity are simply absent from GSC's response."""
        rows = await self._page_query(
            site_url, page_url, start, end, dimensions=["date"], row_limit=500,
        )
        return sorted(
            (
                {
                    "date": r["keys"][0],
                    "clicks": int(r.get("clicks", 0)),
                    "impressions": int(r.get("impressions", 0)),
                }
                for r in rows
            ),
            key=lambda r: r["date"],
        )

    async def get_page_query_details(
        self, site_url: str, page_url: str, start: str, end: str, limit: int = 100,
    ) -> list[dict[str, Any]]:
        """Every search query this page appeared for, with CTR and position.

        Sorted by impressions here rather than relying on the API's ordering:
        the queries worth acting on are the high-impression ones, which are
        often the ones with zero clicks and so would sit at the bottom of a
        clicks-ordered response.
        """
        rows = await self._page_query(
            site_url, page_url, start, end, dimensions=["query"], row_limit=limit,
        )
        queries = [
            {
                "query": r["keys"][0],
                "clicks": int(r.get("clicks", 0)),
                "impressions": int(r.get("impressions", 0)),
                "ctr": round(r.get("ctr", 0) * 100, 2),
                "position": round(r.get("position", 0), 1),
            }
            for r in rows
        ]
        return sorted(queries, key=lambda q: q["impressions"], reverse=True)

    async def get_page_queries(
        self, site_url: str, page_url: str, days: int = 90
    ) -> list[dict[str, Any]]:
        """Fetch queries for a specific page."""
        end = datetime.now(UTC).date()
        start = end - timedelta(days=days)

        body = {
            "startDate": str(start),
            "endDate": str(end),
            "dimensions": ["query"],
            "dimensionFilterGroups": [
                {
                    "filters": [
                        {
                            "dimension": "page",
                            "operator": "equals",
                            "expression": page_url,
                        }
                    ]
                }
            ],
            "rowLimit": 10,
        }

        async with httpx.AsyncClient(timeout=15.0) as client:
            resp = await client.post(
                f"{_GSC_BASE}/sites/{_encode_site(site_url)}/searchAnalytics/query",
                headers=self._headers(),
                json=body,
            )
            if resp.status_code == 404:
                return []
            resp.raise_for_status()
            data = resp.json()

        return [
            {
                "query": row["keys"][0],
                "clicks": row.get("clicks", 0),
                "impressions": row.get("impressions", 0),
                "position": round(row.get("position", 0), 1),
            }
            for row in data.get("rows", [])
        ]

    async def get_opportunity_queries(
        self, site_url: str, days: int = 90
    ) -> list[dict[str, Any]]:
        """
        Queries ranking positions 4–15 with decent impressions — the opportunity zone.
        Returns sorted by impressions desc.
        """
        queries = await self.get_top_queries(site_url, days=days, limit=200)
        return [
            q for q in queries
            if 4 <= q["position"] <= 15 and q["impressions"] >= 10
        ]


def _encode_site(site_url: str) -> str:
    """URL-encode site URL for GSC API path."""
    from urllib.parse import quote
    return quote(site_url, safe="")
