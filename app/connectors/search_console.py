"""Google Search Console API connector."""
from datetime import datetime, timedelta, timezone
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
        end = datetime.now(timezone.utc).date()
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

    async def get_page_queries(
        self, site_url: str, page_url: str, days: int = 90
    ) -> list[dict[str, Any]]:
        """Fetch queries for a specific page."""
        end = datetime.now(timezone.utc).date()
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
