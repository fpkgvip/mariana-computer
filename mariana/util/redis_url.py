"""Shared Redis URL transport-policy helpers."""

from __future__ import annotations

import ipaddress
from urllib.parse import urlparse

_LOCAL_REDIS_HOSTS: frozenset[str] = frozenset({"localhost", "127.0.0.1", "::1", "redis"})


def _is_local_redis_hostname(hostname: str) -> bool:
    host = hostname.strip().lower()
    if not host:
        return False
    if host in _LOCAL_REDIS_HOSTS:
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def assert_local_or_tls(url: str | None, *, surface: str) -> None:
    """Require TLS for non-local Redis URLs.

    ``redis://`` is allowed only for exact local hostnames. ``rediss://`` is
    accepted for any valid hostname. Missing / malformed hostnames fail closed.
    Empty / None URLs are tolerated for test and local no-client callsites.
    """
    if not url:
        return

    parsed = urlparse(url.strip())
    scheme = (parsed.scheme or "").lower()
    hostname = parsed.hostname
    if hostname is None:
        raise ValueError(f"Malformed Redis URL for {surface}; missing hostname in {url!r}")

    if scheme == "rediss":
        return
    if scheme != "redis":
        raise ValueError(
            f"Malformed Redis URL for {surface}; expected redis:// or rediss://, got {url!r}"
        )
    if _is_local_redis_hostname(hostname):
        return
    raise ValueError(
        f"Remote Redis URLs must use rediss:// (TLS) for {surface}; got {url!r}"
    )
