"""Per-request credential isolation (vendor-mcp SOP S3).

The gateway calls this stateless service concurrently on behalf of many
tenants, each carrying its own Calendly Personal Access Token in a
request header. The credential is never read from an environment
variable or a file, never cached across requests, and never shared
through a module-level variable -- only a ``contextvars.ContextVar``
set for the lifetime of one request and reset in a ``finally`` block.
"""

from __future__ import annotations

import contextvars
import json

from starlette.types import ASGIApp, Receive, Scope, Send

# Header name is part of the public contract: it must match the README's
# "Authentication headers" table and the vendor registration exactly.
CREDENTIAL_HEADER = "X-Calendly-Api-Key"
_CREDENTIAL_HEADER_LOWER = CREDENTIAL_HEADER.lower().encode("latin-1")

# Only the Streamable HTTP endpoint needs a credential; /health must stay
# reachable with no Header at all (SOP S1.1).
_PROTECTED_PATH = "/mcp"

_credential: contextvars.ContextVar[str | None] = contextvars.ContextVar("calendly_api_key", default=None)


def get_credential() -> str | None:
    """Read the current request's Calendly Personal Access Token, if any."""
    return _credential.get()


def _unauthorized_body() -> bytes:
    payload = {
        "error": {
            "code": "unauthorized",
            "message": f"Missing required header: {CREDENTIAL_HEADER}",
            "retryable": False,
        }
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


class CredentialMiddleware:
    """Pure ASGI middleware -- not ``BaseHTTPMiddleware``, which buffers the
    body and would break the Streamable HTTP response (SSE / chunked)."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or scope.get("path") != _PROTECTED_PATH:
            await self.app(scope, receive, send)
            return

        token: str | None = None
        for key, value in scope.get("headers", []):
            if key == _CREDENTIAL_HEADER_LOWER:
                token = value.decode("latin-1")
                break

        if not token:
            body = _unauthorized_body()
            await send(
                {
                    "type": "http.response.start",
                    "status": 401,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send({"type": "http.response.body", "body": body})
            return

        reset_token = _credential.set(token)
        try:
            await self.app(scope, receive, send)
        finally:
            _credential.reset(reset_token)
