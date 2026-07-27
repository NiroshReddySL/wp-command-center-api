"""Bounded retry with backoff for outbound HTTP calls.

One transient 429/503 or network blip should not fail a whole agent run.
Retries transport errors and retryable status codes with linear backoff,
honouring `Retry-After` when a server sends one.
"""
import asyncio
import logging
from collections.abc import Awaitable, Callable

import httpx

logger = logging.getLogger(__name__)

RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


async def request_with_retries(
    send: Callable[[], Awaitable[httpx.Response]],
    *,
    retries: int = 2,
    backoff_seconds: float = 0.5,
    what: str = "request",
) -> httpx.Response:
    """Call `send()` until it returns a non-retryable response.

    Raises the last transport error if every attempt fails to connect.
    A response with a retryable status is returned as-is after the final
    attempt — callers keep their existing raise_for_status handling.
    """
    last_exc: Exception | None = None

    for attempt in range(retries + 1):
        try:
            resp = await send()
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_exc = exc
            if attempt < retries:
                await asyncio.sleep(backoff_seconds * (attempt + 1))
                continue
            raise

        if resp.status_code in RETRYABLE_STATUSES and attempt < retries:
            retry_after = resp.headers.get("Retry-After")
            try:
                delay = min(float(retry_after), 10.0) if retry_after else backoff_seconds * (attempt + 1)
            except ValueError:
                delay = backoff_seconds * (attempt + 1)
            logger.debug("%s got HTTP %d — retrying in %.1fs", what, resp.status_code, delay)
            await asyncio.sleep(delay)
            continue

        return resp

    raise last_exc if last_exc else RuntimeError(f"{what}: retries exhausted")
