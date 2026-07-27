"""Unit tests for the in-process rate limiter."""
import pytest
from fastapi import HTTPException, Request

from app.security.rate_limit import rate_limit


def _request(ip: str) -> Request:
    return Request({"type": "http", "client": (ip, 12345), "headers": []})


class TestRateLimit:
    def test_allows_up_to_limit_then_429(self) -> None:
        limiter = rate_limit("test-scope-a", times=3, seconds=60)
        req = _request("10.1.1.1")

        for _ in range(3):
            limiter(req)  # no exception

        with pytest.raises(HTTPException) as exc:
            limiter(req)
        assert exc.value.status_code == 429
        assert "Retry-After" in (exc.value.headers or {})

    def test_limits_are_per_client_ip(self) -> None:
        limiter = rate_limit("test-scope-b", times=1, seconds=60)
        limiter(_request("10.2.2.1"))

        # A different IP has its own window
        limiter(_request("10.2.2.2"))

        with pytest.raises(HTTPException):
            limiter(_request("10.2.2.1"))
