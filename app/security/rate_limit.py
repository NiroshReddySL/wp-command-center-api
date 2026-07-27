"""Minimal in-process rate limiting for expensive or abusable endpoints.

Sliding-window counter keyed by (scope, client IP). In-memory by design —
suits the current single-process deployment; swap for a Redis-backed limiter
when scaling to multiple workers.
"""
import time
from collections import defaultdict, deque
from typing import Callable

from fastapi import HTTPException, Request

_hits: dict[str, deque[float]] = defaultdict(deque)


def rate_limit(scope: str, times: int, seconds: int) -> Callable[[Request], None]:
    """Dependency factory: allow `times` requests per `seconds` per client IP."""

    def dependency(request: Request) -> None:
        client_ip = request.client.host if request.client else "unknown"
        key = f"{scope}:{client_ip}"
        now = time.monotonic()
        window = _hits[key]

        while window and now - window[0] > seconds:
            window.popleft()

        if len(window) >= times:
            raise HTTPException(
                status_code=429,
                detail=f"Rate limit exceeded — max {times} requests per {seconds}s",
                headers={"Retry-After": str(seconds)},
            )
        window.append(now)

    return dependency


# Shared limiters
login_limiter = rate_limit("login", times=5, seconds=60)
ai_limiter = rate_limit("ai", times=20, seconds=60)
job_limiter = rate_limit("jobs", times=6, seconds=60)
