"""The fixed structured-error vocabulary every tool returns on failure.

Per the vendor-mcp SOP S4: tools never raise, they return a JSON string.
``ToolError`` is the one exception type every tool-level try/except
catches and turns into that envelope.
"""

from __future__ import annotations

import json
from typing import Any, Literal

ErrorCode = Literal[
    "not_configured",
    "unauthorized",
    "not_found",
    "invalid_argument",
    "rate_limited",
    "upstream_error",
]


class ToolError(Exception):
    """A structured, Agent-facing error. Never let a raw exception escape a tool."""

    def __init__(self, code: ErrorCode, message: str, retryable: bool = False) -> None:
        super().__init__(message)
        self.code: ErrorCode = code
        self.message = message
        self.retryable = retryable

    def envelope(self) -> dict[str, Any]:
        return {"error": {"code": self.code, "message": self.message, "retryable": self.retryable}}

    def to_json(self) -> str:
        return json.dumps(self.envelope(), ensure_ascii=False, separators=(",", ":"))


def from_calendly_response(status_code: int, body: dict[str, Any] | None) -> ToolError:
    """Map a Calendly HTTP status + error body to the fixed error vocabulary.

    Calendly's own error body shape is ``{"title", "message", "details": [{"parameter",
    "message", "code"}]}``. The mapping below is by HTTP status, per the SOP S4.2 table;
    ``details[0].message`` is appended when present for a more specific message.
    """
    body = body or {}
    base_message = body.get("message") or body.get("title") or f"Calendly API returned HTTP {status_code}"
    details = body.get("details")
    if isinstance(details, list) and details:
        first = details[0]
        if isinstance(first, dict) and first.get("message"):
            base_message = f"{base_message}: {first['message']}"

    if status_code in (401, 403):
        return ToolError("unauthorized", base_message, False)
    if status_code == 404:
        return ToolError("not_found", base_message, False)
    if status_code in (400, 422):
        return ToolError("invalid_argument", base_message, False)
    if status_code == 429:
        return ToolError("rate_limited", base_message, True)
    if status_code >= 500:
        return ToolError("upstream_error", base_message, True)
    # Any other 4xx (e.g. 424 "unable to access external calendar") is a
    # vendor-side condition this service cannot resolve by retrying.
    return ToolError("upstream_error", base_message, False)


def dumps(payload: Any) -> str:
    """Compact, non-ASCII-escaping JSON -- the only serialization every tool uses (SOP S2.5)."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
