"""Thin async wrapper around the Calendly REST API (https://api.calendly.com).

One instance is created and reused for the duration of a single tool
call (SOP S5 "connection reuse"), never cached across requests or
tenants (SOP S3.4). Timeouts, bounded retries with backoff, and a
wall-clock budget keep one call well under the SOP's 120s ceiling.
"""

from __future__ import annotations

import asyncio
import time
from types import TracebackType
from typing import Any

import httpx

from .errors import ToolError, from_calendly_response
from .middleware import get_credential
from .settings import settings

_RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})
_MAX_ATTEMPTS = 3
_TOTAL_BUDGET_SECONDS = 110.0
_MAX_BACKOFF_SECONDS = 8.0


class CalendlyClient:
    """One client per tool call. Never store this on a module-level variable."""

    def __init__(self, *, transport: httpx.AsyncBaseTransport | None = None) -> None:
        token = get_credential()
        if not token:
            # The middleware already rejects requests with no header before a
            # tool ever runs; this only guards against a tool being called
            # outside that path (e.g. a future non-HTTP entry point).
            raise ToolError("not_configured", "No Calendly credential in the current request context", False)
        self._client = httpx.AsyncClient(
            base_url=settings.calendly_base_url,
            headers={"Authorization": f"Bearer {token}"},
            timeout=httpx.Timeout(connect=5.0, read=30.0, write=30.0, pool=30.0),
            transport=transport,
        )

    async def __aenter__(self) -> "CalendlyClient":
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._client.aclose()

    async def request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        """Call one Calendly endpoint, retrying 429/5xx/network errors with backoff.

        Raises ``ToolError`` on any failure (including after exhausting
        retries); never returns a partial result in place of an error.
        """
        start = time.monotonic()
        last_error: ToolError | None = None

        for attempt in range(1, _MAX_ATTEMPTS + 1):
            response: httpx.Response | None = None
            try:
                response = await self._client.request(method, path, **kwargs)
            except httpx.TimeoutException as exc:
                last_error = ToolError("upstream_error", f"Timed out calling Calendly: {exc}", True)
            except httpx.HTTPError as exc:
                last_error = ToolError("upstream_error", f"Network error calling Calendly: {exc}", True)

            if response is not None:
                if response.status_code < 400:
                    if response.status_code == 204 or not response.content:
                        return {}
                    return response.json()

                last_error = from_calendly_response(response.status_code, _safe_json(response))
                if response.status_code not in _RETRYABLE_STATUSES:
                    raise last_error

            if attempt == _MAX_ATTEMPTS:
                raise last_error  # type: ignore[misc]

            delay = _retry_after_seconds(response)
            if delay is None:
                delay = min(2.0**attempt, _MAX_BACKOFF_SECONDS)
            elapsed = time.monotonic() - start
            if elapsed + delay >= _TOTAL_BUDGET_SECONDS:
                raise last_error  # type: ignore[misc]
            await asyncio.sleep(delay)

        raise last_error  # type: ignore[misc]  # pragma: no cover -- loop always returns/raises above


def _safe_json(response: httpx.Response) -> dict[str, Any] | None:
    try:
        return response.json()
    except ValueError:
        return None


def _retry_after_seconds(response: httpx.Response | None) -> float | None:
    if response is None:
        return None
    value = response.headers.get("Retry-After")
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        return None
