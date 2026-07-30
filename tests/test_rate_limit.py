"""Unit tests for the in-process rate limiter."""
import time
from collections import deque

import pytest
from fastapi import HTTPException, Request

from app.security.rate_limit import _client_ip, rate_limit, rescan_limiter


def _request(ip: str, forwarded_for: str | None = None) -> Request:
    headers = (
        [(b"x-forwarded-for", forwarded_for.encode())] if forwarded_for else []
    )
    return Request({"type": "http", "client": (ip, 12345), "headers": headers})


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

    def test_scopes_do_not_share_a_budget(self) -> None:
        """Regression: a single-post rescan used to share the "ai" scope with
        whole-site jobs, so triaging a content list could be 429'd by an
        unrelated funnel run. Distinct scopes must not deplete each other."""
        one = rate_limit("test-scope-c1", times=1, seconds=60)
        two = rate_limit("test-scope-c2", times=1, seconds=60)
        req = _request("10.3.3.1")

        one(req)
        two(req)  # same IP, different scope — must still be allowed

        with pytest.raises(HTTPException):
            one(req)

    def test_retry_after_reflects_time_until_a_slot_frees(self) -> None:
        limiter = rate_limit("test-scope-d", times=1, seconds=60)
        req = _request("10.4.4.1")
        limiter(req)

        with pytest.raises(HTTPException) as exc:
            limiter(req)
        retry_after = int((exc.value.headers or {})["Retry-After"])
        # The window just opened, so a slot frees in ~the full window — and
        # it must never be 0 (which would invite an instant, futile retry).
        assert 1 <= retry_after <= 60
        assert f"{retry_after}s" in exc.value.detail

class TestSweep:
    """`_hits` is process-lifetime state keyed by (scope, IP). An IP that
    never comes back is never touched again, so its entry can only be
    reclaimed by a periodic pass — not by cleanup on access."""

    def test_reclaims_keys_no_limiter_can_still_care_about(self) -> None:
        import app.security.rate_limit as rl

        now = time.monotonic()
        rl._hits["test-sweep:stale"] = deque([now - (rl._MAX_WINDOW_SECONDS + 60)])
        rl._hits["test-sweep:empty"] = deque()
        rl._last_sweep = 0.0  # force the amortized sweep to run

        rl._sweep(now)

        assert "test-sweep:stale" not in rl._hits
        assert "test-sweep:empty" not in rl._hits

    def test_leaves_active_windows_alone(self) -> None:
        import app.security.rate_limit as rl

        now = time.monotonic()
        rl._hits["test-sweep:active"] = deque([now - 5])
        rl._last_sweep = 0.0

        rl._sweep(now)

        assert "test-sweep:active" in rl._hits
        rl._hits.pop("test-sweep:active", None)

    def test_is_amortized_not_run_on_every_request(self) -> None:
        import app.security.rate_limit as rl

        now = time.monotonic()
        rl._last_sweep = now  # a sweep just happened
        rl._hits["test-sweep:recent"] = deque([now - (rl._MAX_WINDOW_SECONDS + 60)])

        rl._sweep(now)  # too soon — must be a no-op

        assert "test-sweep:recent" in rl._hits
        rl._hits.pop("test-sweep:recent", None)


class TestClientIp:
    """Behind a reverse proxy every request shares the proxy's socket IP, so
    keying on it alone would rate-limit the whole team as one caller. But
    X-Forwarded-For is client-settable, so trusting it when NOT behind a
    proxy would let anyone forge a fresh IP per request and bypass limits
    entirely — hence the explicit setting."""

    def test_ignores_forwarded_header_by_default(self, monkeypatch) -> None:
        from app.config import settings

        monkeypatch.setattr(settings, "TRUST_PROXY_HEADERS", False)
        assert _client_ip(_request("10.0.0.9", forwarded_for="1.2.3.4")) == "10.0.0.9"

    def test_uses_forwarded_header_when_trusted(self, monkeypatch) -> None:
        from app.config import settings

        monkeypatch.setattr(settings, "TRUST_PROXY_HEADERS", True)
        assert _client_ip(_request("10.0.0.9", forwarded_for="1.2.3.4")) == "1.2.3.4"

    def test_takes_the_original_client_from_a_proxy_chain(self, monkeypatch) -> None:
        from app.config import settings

        monkeypatch.setattr(settings, "TRUST_PROXY_HEADERS", True)
        req = _request("10.0.0.9", forwarded_for="1.2.3.4, 70.41.3.18, 150.172.238.178")
        assert _client_ip(req) == "1.2.3.4"

    def test_falls_back_to_socket_ip_when_header_absent(self, monkeypatch) -> None:
        from app.config import settings

        monkeypatch.setattr(settings, "TRUST_PROXY_HEADERS", True)
        assert _client_ip(_request("10.0.0.9")) == "10.0.0.9"


class TestRescanLimiter:
    def test_rescan_has_a_larger_budget_than_the_shared_ai_scope(self) -> None:
        """The reported symptom: rescan is a per-row button, so a user
        triaging a content list fires many in quick succession."""
        req = _request("10.6.6.1")
        for _ in range(40):
            rescan_limiter(req)

        with pytest.raises(HTTPException) as exc:
            rescan_limiter(req)
        assert exc.value.status_code == 429
