"""Minimal in-process rate limiting for expensive or abusable endpoints.

Sliding-window counter keyed by (scope, client IP). In-memory by design —
suits the current single-process deployment; swap for a Redis-backed limiter
when scaling to multiple workers.
"""
import math
import time
from collections import defaultdict, deque
from collections.abc import Callable

from fastapi import HTTPException, Request

from app.config import settings

_hits: dict[str, deque[float]] = defaultdict(deque)

# Longest window any limiter below uses, with generous headroom: a key whose
# newest hit is older than this cannot matter to any of them.
_MAX_WINDOW_SECONDS = 3600
_SWEEP_EVERY_SECONDS = 300
_last_sweep = 0.0


def _sweep(now: float) -> None:
    """Drop keys no limiter can still care about.

    Per-key cleanup on access can't solve this: `_hits` is keyed by
    (scope, IP), and the entry for an IP that never returns is never
    touched again — so its stale timestamps would sit in memory for the
    life of the process. Only a periodic pass over all keys reclaims them.
    Amortized to once per _SWEEP_EVERY_SECONDS so it stays off the hot path.
    """
    global _last_sweep
    if now - _last_sweep < _SWEEP_EVERY_SECONDS:
        return
    _last_sweep = now
    dead = [k for k, w in _hits.items() if not w or now - w[-1] > _MAX_WINDOW_SECONDS]
    for k in dead:
        _hits.pop(k, None)


def _client_ip(request: Request) -> str:
    """The caller's real IP.

    `request.client.host` is the SOCKET peer — behind any reverse proxy
    (nginx, an ALB, Cloudflare) that's the proxy's address, identical for
    every user, so one busy user would rate-limit the entire team out of
    the product. X-Forwarded-For carries the real client, but it's a
    client-settable header: trusting it unconditionally lets anyone evade
    every limit here by forging a new IP per request. So it's honoured only
    when TRUST_PROXY_HEADERS says we're actually behind a proxy that
    overwrites it.
    """
    if settings.TRUST_PROXY_HEADERS:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            # Left-most entry is the original client; the rest are hops.
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def rate_limit(scope: str, times: int, seconds: int) -> Callable[[Request], None]:
    """Dependency factory: allow `times` requests per `seconds` per client IP.

    `scope` is the budget's identity — endpoints sharing a scope share one
    allowance, so give unrelated operations distinct scopes unless they're
    genuinely meant to compete for the same budget.
    """

    def dependency(request: Request) -> None:
        key = f"{scope}:{_client_ip(request)}"
        now = time.monotonic()
        _sweep(now)
        window = _hits[key]

        while window and now - window[0] > seconds:
            window.popleft()

        if len(window) >= times:
            # Seconds until the oldest hit ages out — i.e. when a slot
            # actually frees up. The window length would be a blunter
            # over-estimate that tells callers to wait longer than needed.
            retry_after = max(1, math.ceil(seconds - (now - window[0])))
            raise HTTPException(
                status_code=429,
                detail=f"Rate limit exceeded — max {times} requests per {seconds}s. Try again in {retry_after}s.",
                headers={"Retry-After": str(retry_after)},
            )
        window.append(now)

    return dependency


# ── Shared limiters ───────────────────────────────────────────────────────────
login_limiter = rate_limit("login", times=5, seconds=60)
# Heavyweight, mostly site-wide AI/analytics jobs: prediction regeneration,
# a flow-category funnel run, a whole-post AI rewrite.
ai_limiter = rate_limit("ai", times=20, seconds=60)
# Single-post rescan gets its OWN budget rather than sharing "ai": it's a
# per-row button in the content-health table, so triaging a list of posts
# legitimately fires many in a row — and there's no reason those clicks
# should be starved by an unrelated 90-day funnel run happening elsewhere
# in the app.
rescan_limiter = rate_limit("rescan", times=40, seconds=60)
job_limiter = rate_limit("jobs", times=6, seconds=60)
