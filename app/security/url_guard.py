"""SSRF guard for user-supplied URLs.

The server fetches user-provided site URLs (WordPress REST API, page scans).
Without validation an attacker could point a "site" at internal services or
cloud metadata endpoints. `ensure_public_url` rejects anything that is not a
public http(s) host, unless ALLOW_PRIVATE_URLS is enabled for local dev.
"""
import asyncio
import ipaddress
import socket
from urllib.parse import urlparse

from fastapi import HTTPException

from app.config import settings


def _is_public_address(ip_str: str) -> bool:
    ip = ipaddress.ip_address(ip_str)
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local       # includes 169.254.169.254 cloud metadata
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


async def ensure_public_url(url: str) -> None:
    """Raise 422 if `url` is not a resolvable public http(s) URL."""
    parsed = urlparse(url)

    if parsed.scheme not in ("http", "https"):
        raise HTTPException(status_code=422, detail="Site URL must use http or https")
    if not parsed.hostname:
        raise HTTPException(status_code=422, detail="Site URL has no hostname")

    if settings.ALLOW_PRIVATE_URLS:
        return

    try:
        infos = await asyncio.to_thread(
            socket.getaddrinfo, parsed.hostname, None, proto=socket.IPPROTO_TCP
        )
    except socket.gaierror:
        raise HTTPException(status_code=422, detail="Site URL hostname does not resolve")

    for info in infos:
        if not _is_public_address(info[4][0]):
            raise HTTPException(
                status_code=422,
                detail="Site URL resolves to a private or reserved address",
            )
