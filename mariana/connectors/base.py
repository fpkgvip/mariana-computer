"""
Base connector class for all Mariana data connectors.

Provides common HTTP client, retry logic, caching, and error handling
that all concrete connectors inherit.
"""

from __future__ import annotations

import hashlib
import ipaddress
import socket
from abc import ABC, abstractmethod
from typing import Any
from urllib.parse import urlparse, urlsplit, urlunsplit

import httpx
import structlog
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

logger = structlog.get_logger(__name__)


class ConnectorError(Exception):
    """Base error for all connector-level failures."""


class RateLimitError(ConnectorError):
    """Raised when the upstream API returns HTTP 429."""


class SSRFBlockedError(ConnectorError):
    """Raised when a redirect or request targets an internal/private address."""


def _is_internal_host(host: str | None) -> bool:
    """Return True if the host is a loopback, link-local, or RFC1918 address."""
    if not host:
        return True
    h = host.strip().lower()
    if h in ("localhost", "ip6-localhost", "ip6-loopback", ""):
        return True
    # Try to parse directly as an IP
    try:
        ip = ipaddress.ip_address(h.strip("[]"))
        return (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        )
    except ValueError:
        pass
    # Resolve hostname to IPs and check each one
    try:
        infos = socket.getaddrinfo(h, None)
    except socket.gaierror:
        # Unresolvable hostname — treat as unsafe by default? No — let the
        # underlying request fail naturally rather than introduce false positives.
        return False
    for info in infos:
        sockaddr = info[4]
        try:
            ip = ipaddress.ip_address(sockaddr[0])
        except (ValueError, IndexError):
            continue
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            return True
    return False


def _redact_url(url: str) -> str:
    """Redact query parameters from a URL for safe logging. (M-06)"""
    try:
        parts = urlsplit(url)
        redacted = parts._replace(query="[redacted]" if parts.query else "")
        return urlunsplit(redacted)
    except Exception:
        return "[unparsable-url]"


async def _ssrf_redirect_hook(response: httpx.Response) -> None:
    """Block redirects to internal/private addresses (C-01).

    httpx invokes response event hooks for every response in a redirect chain.
    When it sees a 3xx, we validate the Location header before httpx follows it.
    """
    if response.status_code in (301, 302, 303, 307, 308):
        loc = response.headers.get("location")
        if not loc:
            return
        try:
            # Resolve relative redirect against the request URL
            target = httpx.URL(str(response.request.url)).join(loc)
        except Exception as exc:
            raise SSRFBlockedError(f"Invalid redirect target: {loc!r}") from exc
        if _is_internal_host(target.host):
            raise SSRFBlockedError(
                f"Redirect to internal address blocked: host={target.host!r}"
            )


class BaseConnector(ABC):
    """
    Abstract base class for all Mariana data connectors.

    Subclasses should call super().__init__(config, cache) and use
    self._request() for every outbound HTTP call so they get automatic
    retry, rate-limit handling, and cache integration for free.

    Args:
        config: Application config object with API keys and settings.
        cache:  Optional async cache object exposing get/set(key, value, ttl).
    """

    def __init__(self, config: Any, cache: Any | None = None) -> None:
        self.config = config
        self.cache = cache
        # C-01 fix: Follow redirects but validate each hop via an event hook so
        # an attacker-controlled 3xx cannot redirect us to internal/private
        # addresses (AWS metadata, RFC1918, loopback, link-local).
        self.client = httpx.AsyncClient(
            timeout=30.0,
            follow_redirects=True,
            event_hooks={"response": [_ssrf_redirect_hook]},
        )
        self._closed: bool = False
        self._log = logger.bind(connector=self.__class__.__name__)

    async def close(self) -> None:
        """Release the underlying HTTP client."""
        if self._closed:
            return
        self._closed = True
        await self.client.aclose()

    async def __aenter__(self) -> "BaseConnector":
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    # ------------------------------------------------------------------
    # HTTP helpers
    # ------------------------------------------------------------------

    def __del__(self) -> None:
        # BUG-011 fix: warn if the connector was garbage-collected without being
        # properly closed, which would silently leak the httpx connection pool.
        if not self._closed:
            import warnings
            warnings.warn(
                f"{self.__class__.__name__} was not properly closed. "
                "Use 'async with' or call 'await connector.close()'.",
                ResourceWarning,
                stacklevel=2,
            )

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=16),
        # BUG-010 fix: add ConnectorError to the retry list so transient network
        # errors (timeouts, TCP resets) that are wrapped as ConnectorError are also
        # retried — not just HTTP status errors and explicit rate-limit responses.
        retry=retry_if_exception_type((httpx.HTTPStatusError, RateLimitError, ConnectorError)),
        reraise=True,
    )
    async def _request(self, method: str, url: str, **kwargs: Any) -> dict:
        """
        Execute an HTTP request with automatic retry on transient errors.

        Retries up to 3 times with exponential back-off for HTTP errors
        and explicit rate-limit responses.  All other exceptions propagate
        immediately.

        Args:
            method: HTTP verb ("GET", "POST", …).
            url:    Full request URL.
            **kwargs: Passed verbatim to httpx.AsyncClient.request().

        Returns:
            Parsed JSON response body as a dict.

        Raises:
            RateLimitError: When the upstream returns HTTP 429.
            httpx.HTTPStatusError: For other non-2xx responses after retries.
            ConnectorError: For non-HTTP failures (network, timeout, etc.).
        """
        self._log.debug("http_request", method=method, url=_redact_url(url))
        try:
            resp = await self.client.request(method, url, **kwargs)
        except httpx.TimeoutException as exc:
            raise ConnectorError(f"Request timed out: {url}") from exc
        except httpx.RequestError as exc:
            raise ConnectorError(f"Network error for {url}: {exc}") from exc

        if resp.status_code == 429:
            self._log.warning("rate_limited", url=url)
            raise RateLimitError(f"Rate limited: {url}")

        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError:
            self._log.error(
                "http_error",
                url=_redact_url(url),
                status_code=resp.status_code,
                body=resp.text[:500],
            )
            raise

        return resp.json()

    @retry(
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=16),
        # BUG-010 fix (same as _request): add ConnectorError so network/timeout
        # errors are retried consistently across both HTTP helper methods.
        retry=retry_if_exception_type((httpx.HTTPStatusError, RateLimitError, ConnectorError)),
        reraise=True,
    )
    async def _request_text(self, method: str, url: str, **kwargs: Any) -> str:
        """
        Like _request() but returns the raw response text instead of JSON.

        Useful for fetching filing documents, HTML pages, etc.
        """
        self._log.debug("http_request_text", method=method, url=_redact_url(url))
        try:
            resp = await self.client.request(method, url, **kwargs)
        except httpx.TimeoutException as exc:
            raise ConnectorError(f"Request timed out: {url}") from exc
        except httpx.RequestError as exc:
            raise ConnectorError(f"Network error for {url}: {exc}") from exc

        if resp.status_code == 429:
            raise RateLimitError(f"Rate limited: {url}")

        try:
            resp.raise_for_status()
        except httpx.HTTPStatusError:
            self._log.error(
                "http_error",
                url=_redact_url(url),
                status_code=resp.status_code,
            )
            raise

        return resp.text

    # ------------------------------------------------------------------
    # Cache helpers
    # ------------------------------------------------------------------

    def _cache_key(self, *parts: str) -> str:
        """
        Build a deterministic cache key from arbitrary string parts.

        Uses SHA-256 so that long or special-character values are safe
        to use as cache identifiers.
        """
        raw = ":".join(parts)
        return hashlib.sha256(raw.encode()).hexdigest()

    def _hash_url(self, url: str) -> str:
        """Return a SHA-256 hex digest of the given URL string."""
        return hashlib.sha256(url.encode()).hexdigest()

    async def _cache_get(self, key: str) -> Any | None:
        """Return a cached value or None if the cache is absent / cold."""
        if self.cache is None:
            return None
        try:
            return await self.cache.get(key)
        except Exception as exc:  # cache should never crash the connector
            self._log.warning("cache_get_error", key=key, error=str(exc))
            return None

    async def _cache_set(self, key: str, value: Any, ttl: int) -> None:
        """Store a value in the cache with the given TTL (seconds)."""
        if self.cache is None:
            return
        try:
            await self.cache.set(key, value, ttl=ttl)
        except Exception as exc:
            self._log.warning("cache_set_error", key=key, error=str(exc))

    # ------------------------------------------------------------------
    # Subclass contract
    # ------------------------------------------------------------------

    @abstractmethod
    async def search_for_topic(self, topic: str) -> list[dict]:
        """
        High-level entry point used by the orchestrator.

        Implementations should:
          1. Extract relevant search terms / tickers from `topic`.
          2. Fan-out across relevant endpoints.
          3. Return a list of structured finding dicts ready for the
             orchestrator to process.

        Args:
            topic: Free-form research topic string.

        Returns:
            List of finding dicts (schema varies by connector).
        """
        ...
