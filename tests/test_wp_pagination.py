"""WordPressConnector collection fetching — full crawl, incremental
modified-since crawl, and live-ID listing. This is the machinery that makes
"crawl the entire site, not just the first 100 items" both complete and
cheap for enterprise-scale sites.
"""
import httpx
import pytest

from app.connectors.wordpress import WordPressConnector, parse_wp_datetime

FIELDS = "id,modified_gmt"


class _FakeWP(WordPressConnector):
    """Connector with per-page-number scripted responses — deterministic
    regardless of the order concurrent requests are dispatched in."""

    def __init__(self, pages_by_number: dict[int, httpx.Response]) -> None:
        super().__init__("https://fake.example.com")
        self._pages = pages_by_number
        self.requested_pages: list[int] = []

    async def _get(self, url: str, params: dict | None = None) -> httpx.Response:
        page = (params or {}).get("page", 1)
        self.requested_pages.append(page)
        return self._pages.get(page, self._pages[max(self._pages)])

    @property
    def calls(self) -> int:
        return len(self.requested_pages)


def _items(count: int, start_id: int = 0, modified: str | None = None) -> list[dict]:
    return [
        {"id": start_id + i, **({"modified_gmt": modified} if modified else {})}
        for i in range(count)
    ]


def _resp(
    items: list[dict], status: int = 200, total: int | None = None, total_pages: int | None = None,
) -> httpx.Response:
    headers = {}
    if total is not None:
        headers["X-WP-Total"] = str(total)
    if total_pages is not None:
        headers["X-WP-TotalPages"] = str(total_pages)
    return httpx.Response(
        status, json=items, headers=headers,
        request=httpx.Request("GET", "https://fake.example.com"),
    )


class TestFetchAll:
    @pytest.mark.asyncio
    async def test_single_page_needs_no_extra_requests(self) -> None:
        wp = _FakeWP({1: _resp(_items(37), total=37, total_pages=1)})
        items, total = await wp.fetch_all("posts", FIELDS)
        assert len(items) == 37
        assert total == 37
        assert wp.calls == 1

    @pytest.mark.asyncio
    async def test_spans_multiple_pages_using_total_pages_header(self) -> None:
        # 250 items across 3 pages — first page's headers tell fetch_all
        # exactly how many more requests to fire, all concurrently.
        wp = _FakeWP({
            1: _resp(_items(100, 0), total=250, total_pages=3),
            2: _resp(_items(100, 100)),
            3: _resp(_items(50, 200)),
        })
        items, total = await wp.fetch_all("posts", FIELDS)
        assert len(items) == 250
        assert total == 250
        assert wp.calls == 3
        assert {i["id"] for i in items} == set(range(250))

    @pytest.mark.asyncio
    async def test_late_page_400_treated_as_empty_not_fatal(self) -> None:
        wp = _FakeWP({
            1: _resp(_items(100, 0), total=200, total_pages=2),
            2: _resp([], status=400),
        })
        items, total = await wp.fetch_all("posts", FIELDS)
        assert len(items) == 100
        assert total == 200

    @pytest.mark.asyncio
    async def test_hard_ceiling_bounds_pages_requested(self) -> None:
        # A misconfigured/pathological site reporting 10 pages must not blow
        # past the safety ceiling — only enough pages for the ceiling.
        wp = _FakeWP({
            1: _resp(_items(100, 0), total=1000, total_pages=10),
            **{p: _resp(_items(100, p * 100)) for p in range(2, 11)},
        })
        items, _ = await wp.fetch_all("posts", FIELDS, hard_ceiling=300)
        assert len(items) <= 300
        assert wp.calls <= 3


class TestFetchSince:
    @pytest.mark.asyncio
    async def test_no_checkpoint_falls_back_to_full_crawl(self) -> None:
        wp = _FakeWP({1: _resp(_items(10, modified="2026-06-01T00:00:00"), total=10, total_pages=1)})
        items, total = await wp.fetch_since("posts", FIELDS, modified_after=None)
        assert len(items) == 10
        assert total == 10

    @pytest.mark.asyncio
    async def test_stops_at_the_checkpoint_newest_first(self) -> None:
        checkpoint = parse_wp_datetime("2026-06-01T00:00:00")
        # Page 1: 2 items newer than checkpoint, then one AT the checkpoint —
        # must stop there and never request page 2.
        wp = _FakeWP({
            1: _resp(
                _items(1, 1, modified="2026-06-03T00:00:00")
                + _items(1, 2, modified="2026-06-02T00:00:00")
                + _items(1, 3, modified="2026-06-01T00:00:00"),
                total=50,
            ),
            2: _resp(_items(100, 100)),  # would only be hit by a bug
        })
        items, total = await wp.fetch_since("posts", FIELDS, modified_after=checkpoint)
        assert {i["id"] for i in items} == {1, 2}
        assert total == 50
        assert wp.calls == 1

    @pytest.mark.asyncio
    async def test_continues_past_a_full_page_all_newer_than_checkpoint(self) -> None:
        checkpoint = parse_wp_datetime("2026-01-01T00:00:00")
        page1 = _items(100, 0, modified="2026-06-01T00:00:00")
        page2 = _items(5, 100, modified="2026-05-01T00:00:00")
        wp = _FakeWP({1: _resp(page1, total=105), 2: _resp(page2, total=105)})
        items, _ = await wp.fetch_since("posts", FIELDS, modified_after=checkpoint)
        assert len(items) == 105
        assert wp.calls == 2

    @pytest.mark.asyncio
    async def test_stable_site_costs_one_request(self) -> None:
        """The whole point of incremental sync: nothing changed → one call."""
        checkpoint = parse_wp_datetime("2026-06-05T00:00:00")
        wp = _FakeWP({1: _resp(_items(0), total=500)})
        items, total = await wp.fetch_since("posts", FIELDS, modified_after=checkpoint)
        assert items == []
        assert total == 500
        assert wp.calls == 1


class TestFetchLiveIds:
    @pytest.mark.asyncio
    async def test_returns_id_set_across_pages(self) -> None:
        wp = _FakeWP({
            1: _resp(_items(100, 0), total=150, total_pages=2),
            2: _resp(_items(50, 100)),
        })
        ids = await wp.fetch_live_ids("posts")
        assert ids == set(range(150))


def _item_resp(item: dict, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status, json=item, request=httpx.Request("GET", "https://fake.example.com"),
    )


class TestGetPost:
    """Regression coverage for a real incident: posts and pages share one
    global WordPress ID sequence but are served from two DIFFERENT REST
    endpoints. Rescanning a tracked PAGE by ID against the /posts/ endpoint
    always 404s even though the page is live — which briefly caused live
    pages ("About", "Terms of Use") to be wrongly deleted as "confirmed
    gone from WordPress" before this was traced back to the wrong endpoint."""

    @pytest.mark.asyncio
    async def test_page_content_type_hits_pages_endpoint_not_posts(self) -> None:
        calls: list[str] = []

        class _URLSpy(WordPressConnector):
            async def _get(self, url: str, params: dict | None = None) -> httpx.Response:
                calls.append(url)
                return _item_resp({"id": 3290, "title": {"rendered": "About"}})

        wp = _URLSpy("https://www.example.com")
        result = await wp.get_post(3290, content_type="page")
        assert calls == ["https://www.example.com/wp-json/wp/v2/pages/3290"]
        assert result is not None and result["id"] == 3290

    @pytest.mark.asyncio
    async def test_post_content_type_hits_posts_endpoint(self) -> None:
        calls: list[str] = []

        class _URLSpy(WordPressConnector):
            async def _get(self, url: str, params: dict | None = None) -> httpx.Response:
                calls.append(url)
                return _item_resp({"id": 7, "title": {"rendered": "A Post"}})

        wp = _URLSpy("https://www.example.com")
        await wp.get_post(7, content_type="post")
        assert calls == ["https://www.example.com/wp-json/wp/v2/posts/7"]

    @pytest.mark.asyncio
    async def test_404_on_the_correct_endpoint_returns_none(self) -> None:
        class _NotFoundWP(WordPressConnector):
            async def _get(self, url: str, params: dict | None = None) -> httpx.Response:
                return _item_resp({}, status=404)

        wp = _NotFoundWP("https://www.example.com")
        assert await wp.get_post(999, content_type="page") is None


class TestParseWpDatetime:
    def test_naive_gmt_string_becomes_utc_aware(self) -> None:
        dt = parse_wp_datetime("2026-06-01T12:00:00")
        assert dt is not None
        assert dt.tzinfo is not None

    def test_none_and_empty_are_safe(self) -> None:
        assert parse_wp_datetime(None) is None
        assert parse_wp_datetime("") is None

    def test_comparable_across_two_calls(self) -> None:
        earlier = parse_wp_datetime("2026-06-01T00:00:00")
        later = parse_wp_datetime("2026-06-02T00:00:00")
        assert earlier < later
