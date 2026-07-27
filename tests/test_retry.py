"""Unit tests for the outbound-request retry helper."""
import httpx
import pytest

from app.connectors.retry import request_with_retries


def _responses(*statuses: int) -> list[httpx.Response]:
    return [httpx.Response(s, request=httpx.Request("GET", "https://x.test")) for s in statuses]


class TestRequestWithRetries:
    @pytest.mark.asyncio
    async def test_returns_first_success(self) -> None:
        calls = _responses(200)

        async def send() -> httpx.Response:
            return calls.pop(0)

        resp = await request_with_retries(send, backoff_seconds=0)
        assert resp.status_code == 200

    @pytest.mark.asyncio
    async def test_retries_transient_5xx_then_succeeds(self) -> None:
        calls = _responses(503, 200)

        async def send() -> httpx.Response:
            return calls.pop(0)

        resp = await request_with_retries(send, backoff_seconds=0)
        assert resp.status_code == 200
        assert not calls  # both attempts consumed

    @pytest.mark.asyncio
    async def test_returns_final_retryable_status_after_exhaustion(self) -> None:
        calls = _responses(503, 503, 503)

        async def send() -> httpx.Response:
            return calls.pop(0)

        resp = await request_with_retries(send, retries=2, backoff_seconds=0)
        assert resp.status_code == 503

    @pytest.mark.asyncio
    async def test_does_not_retry_client_errors(self) -> None:
        calls = _responses(404, 200)

        async def send() -> httpx.Response:
            return calls.pop(0)

        resp = await request_with_retries(send, backoff_seconds=0)
        assert resp.status_code == 404
        assert len(calls) == 1  # second response never requested

    @pytest.mark.asyncio
    async def test_retries_transport_errors_then_succeeds(self) -> None:
        attempts = {"n": 0}

        async def send() -> httpx.Response:
            attempts["n"] += 1
            if attempts["n"] < 3:
                raise httpx.ConnectError("boom")
            return _responses(200)[0]

        resp = await request_with_retries(send, retries=2, backoff_seconds=0)
        assert resp.status_code == 200
        assert attempts["n"] == 3

    @pytest.mark.asyncio
    async def test_raises_after_transport_errors_exhausted(self) -> None:
        async def send() -> httpx.Response:
            raise httpx.ConnectError("down")

        with pytest.raises(httpx.ConnectError):
            await request_with_retries(send, retries=1, backoff_seconds=0)
