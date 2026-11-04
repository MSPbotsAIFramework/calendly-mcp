"""Unit tests for the 9 Calendly tools: tools/list snapshot + error mapping.

The Calendly API is always mocked here -- these never touch the network
(vendor-mcp SOP S10). ``_install_fake_client`` replaces ``tools.CalendlyClient``
so each tool's request/shaping logic is exercised without a real HTTP call;
``test_client_retries_and_maps_errors`` separately exercises the real
``CalendlyClient`` against a mocked transport to cover retry/backoff and the
status -> error-code mapping end to end.
"""

from __future__ import annotations

import functools
import json
from typing import Any, Callable

import httpx
import pytest

from calendly_mcp import tools as tools_module
from calendly_mcp.client import CalendlyClient
from calendly_mcp.errors import ToolError
from calendly_mcp.middleware import _credential
from calendly_mcp.server import build_mcp

Handler = Callable[[str, str, dict[str, Any]], dict[str, Any]]


class _FakeCalendlyClient:
    """Drop-in replacement for CalendlyClient that never touches the network."""

    def __init__(self, handler: Handler) -> None:
        self._handler = handler

    async def __aenter__(self) -> "_FakeCalendlyClient":
        return self

    async def __aexit__(self, *exc: object) -> None:
        return None

    async def request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        return self._handler(method, path, kwargs)


def _install_fake_client(monkeypatch: pytest.MonkeyPatch, handler: Handler) -> None:
    monkeypatch.setattr(tools_module, "CalendlyClient", functools.partial(_FakeCalendlyClient, handler))


async def _call(name: str, **kwargs: Any) -> dict[str, Any]:
    """Call a registered tool by name through a fresh FastMCP instance and parse its JSON text.

    Every tool registers with ``structured_output=False`` (SOP S4.1: text only), so
    ``call_tool`` returns a bare list of content blocks, not a (content, structured) pair.
    """
    mcp = build_mcp()
    tools = {t.name: t for t in await mcp.list_tools()}
    assert name in tools
    content = await mcp.call_tool(name, kwargs)
    assert len(content) == 1
    return json.loads(content[0].text)


# ---------------------------------------------------------------------------
# tools/list snapshot (SOP S10, S13: signature changes must show up here)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tools_list_snapshot() -> None:
    mcp = build_mcp()
    tool_list = await mcp.list_tools()
    names = {t.name for t in tool_list}
    assert names == {
        "calendly_get_current_user",
        "calendly_list_event_types",
        "calendly_get_event_type_available_times",
        "calendly_list_scheduled_events",
        "calendly_get_scheduled_event",
        "calendly_list_event_invitees",
        "calendly_cancel_scheduled_event",
        "calendly_list_user_busy_times",
        "calendly_create_scheduling_link",
    }

    required_params = {t.name: set(t.inputSchema.get("required", [])) for t in tool_list}
    assert required_params["calendly_get_current_user"] == set()
    assert required_params["calendly_get_event_type_available_times"] == {"event_type", "start_time", "end_time"}
    assert required_params["calendly_get_scheduled_event"] == {"uuid"}
    assert required_params["calendly_cancel_scheduled_event"] == {"uuid"}
    assert required_params["calendly_list_user_busy_times"] == {"user", "start_time", "end_time"}
    assert required_params["calendly_create_scheduling_link"] == {"owner"}

    by_name = {t.name: t for t in tool_list}
    assert by_name["calendly_cancel_scheduled_event"].annotations.destructiveHint is True
    assert by_name["calendly_get_current_user"].annotations.readOnlyHint is True


# ---------------------------------------------------------------------------
# TC-CAL-001 / TC-CAL-002 (AC1)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_event_types_returns_collection(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(method: str, path: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        assert method == "GET"
        assert path == "/event_types"
        return {
            "collection": [
                {
                    "uri": "https://api.calendly.com/event_types/AAA",
                    "name": "30 Minute Meeting",
                    "duration": 30,
                    "scheduling_url": "https://calendly.com/me/30min",
                    "kind": "solo",
                    "active": True,
                }
            ],
            "pagination": {"next_page_token": None},
        }

    _install_fake_client(monkeypatch, handler)
    result = await _call("calendly_list_event_types")
    assert result["has_more"] is False
    assert len(result["collection"]) == 1
    item = result["collection"][0]
    assert {"uri", "name", "duration", "scheduling_url"} <= item.keys()


@pytest.mark.asyncio
async def test_list_event_types_active_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(method: str, path: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        assert kwargs["params"]["active"] is True
        return {
            "collection": [
                {"uri": "u1", "name": "Active one", "duration": 15, "scheduling_url": "s1", "kind": "solo", "active": True},
            ],
            "pagination": {"next_page_token": None},
        }

    _install_fake_client(monkeypatch, handler)
    result = await _call("calendly_list_event_types", active=True)
    assert all(item["active"] is True for item in result["collection"])


# ---------------------------------------------------------------------------
# TC-CAL-003 / TC-CAL-004 (AC2)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_available_times_31_day_window_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    def handler(method: str, path: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        nonlocal called
        called = True
        return {
            "collection": [
                {
                    "status": "available",
                    "invitees_remaining": 1,
                    "start_time": "2026-01-02T15:00:00Z",
                    "scheduling_url": "https://calendly.com/me/30min/2026-01-02T15:00:00Z",
                }
            ]
        }

    _install_fake_client(monkeypatch, handler)
    result = await _call(
        "calendly_get_event_type_available_times",
        event_type="https://api.calendly.com/event_types/AAA",
        start_time="2026-01-01T00:00:00Z",
        end_time="2026-02-01T00:00:00Z",  # exactly 31 days (January has 31 days)
    )
    assert called is True
    assert "error" not in result
    assert len(result["collection"]) == 1


@pytest.mark.asyncio
async def test_available_times_32_day_window_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    def handler(method: str, path: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        nonlocal called
        called = True
        return {"collection": []}

    _install_fake_client(monkeypatch, handler)
    result = await _call(
        "calendly_get_event_type_available_times",
        event_type="https://api.calendly.com/event_types/AAA",
        start_time="2026-01-01T00:00:00Z",
        end_time="2026-02-02T00:00:00Z",
    )
    assert called is False, "must reject before calling Calendly"
    assert result["error"]["code"] == "invalid_argument"


# ---------------------------------------------------------------------------
# TC-CAL-005 (AC3)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_current_user_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(method: str, path: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        assert path == "/users/me"
        return {
            "resource": {
                "uri": "https://api.calendly.com/users/AAA",
                "name": "Jane Doe",
                "email": "jane@example.com",
                "scheduling_url": "https://calendly.com/jane",
                "current_organization": "https://api.calendly.com/organizations/BBB",
                "slug": "jane",
            }
        }

    _install_fake_client(monkeypatch, handler)
    result = await _call("calendly_get_current_user")
    assert result == {
        "uri": "https://api.calendly.com/users/AAA",
        "name": "Jane Doe",
        "email": "jane@example.com",
        "scheduling_url": "https://calendly.com/jane",
        "current_organization": "https://api.calendly.com/organizations/BBB",
    }


# ---------------------------------------------------------------------------
# TC-CAL-006 / TC-CAL-007 (AC4)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_scheduled_events_default_order(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(method: str, path: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        assert kwargs["params"]["sort"] == "start_time:desc"
        return {
            "collection": [
                {
                    "uri": "u1",
                    "name": "Call",
                    "start_time": "2026-02-01T00:00:00Z",
                    "end_time": "2026-02-01T00:30:00Z",
                    "status": "active",
                    "event_type": "et1",
                    "location": {"type": "zoom"},
                }
            ],
            "pagination": {"next_page_token": None},
        }

    _install_fake_client(monkeypatch, handler)
    result = await _call("calendly_list_scheduled_events")
    assert result["collection"][0]["uri"] == "u1"


@pytest.mark.asyncio
async def test_list_scheduled_events_status_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(method: str, path: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        assert kwargs["params"]["status"] == "canceled"
        return {
            "collection": [
                {
                    "uri": "u1",
                    "name": "Call",
                    "start_time": "t1",
                    "end_time": "t2",
                    "status": "canceled",
                    "event_type": "et1",
                    "location": None,
                }
            ],
            "pagination": {"next_page_token": None},
        }

    _install_fake_client(monkeypatch, handler)
    result = await _call("calendly_list_scheduled_events", status="canceled")
    assert all(item["status"] == "canceled" for item in result["collection"])


# ---------------------------------------------------------------------------
# TC-CAL-008 (AC5)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_get_scheduled_event_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(method: str, path: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        assert path == "/scheduled_events/evt-123"
        return {
            "resource": {
                "uri": "https://api.calendly.com/scheduled_events/evt-123",
                "name": "Call",
                "status": "active",
                "start_time": "2026-02-01T00:00:00Z",
                "end_time": "2026-02-01T00:30:00Z",
                "event_type": "https://api.calendly.com/event_types/AAA",
                "location": {"type": "zoom"},
                "invitees_counter": {"total": 1, "active": 1, "limit": 1},
            }
        }

    _install_fake_client(monkeypatch, handler)
    result = await _call("calendly_get_scheduled_event", uuid="evt-123")
    assert result["status"] == "active"
    assert result["start_time"] == "2026-02-01T00:00:00Z"
    assert result["end_time"] == "2026-02-01T00:30:00Z"
    assert result["location"] == {"type": "zoom"}
    assert result["event_type"] == "https://api.calendly.com/event_types/AAA"
    assert result["invitees_counter"]["total"] == 1


# ---------------------------------------------------------------------------
# TC-CAL-010 (AC6)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_list_event_invitees_shape(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(method: str, path: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        assert path == "/scheduled_events/evt-123/invitees"
        return {
            "collection": [
                {"name": "Bob", "email": "bob@example.com", "status": "active", "cancellation": None},
                {
                    "name": "Alice",
                    "email": "alice@example.com",
                    "status": "canceled",
                    "cancellation": {"canceled_by": "Alice", "reason": "conflict"},
                },
            ],
            "pagination": {"next_page_token": None},
        }

    _install_fake_client(monkeypatch, handler)
    result = await _call("calendly_list_event_invitees", uuid="evt-123")
    names = {item["name"]: item for item in result["collection"]}
    assert names["Bob"]["status"] == "active"
    assert names["Bob"]["cancellation_reason"] is None
    assert names["Alice"]["cancellation_reason"] == "conflict"


# ---------------------------------------------------------------------------
# TC-CAL-012 / TC-CAL-013 (AC8)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_busy_times_7_day_window_ok(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    def handler(method: str, path: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        nonlocal called
        called = True
        return {"collection": [{"type": "calendly", "start_time": "t1", "end_time": "t2"}]}

    _install_fake_client(monkeypatch, handler)
    result = await _call(
        "calendly_list_user_busy_times",
        user="https://api.calendly.com/users/AAA",
        start_time="2026-01-01T00:00:00Z",
        end_time="2026-01-08T00:00:00Z",
    )
    assert called is True
    assert "error" not in result


@pytest.mark.asyncio
async def test_busy_times_8_day_window_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    called = False

    def handler(method: str, path: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        nonlocal called
        called = True
        return {"collection": []}

    _install_fake_client(monkeypatch, handler)
    result = await _call(
        "calendly_list_user_busy_times",
        user="https://api.calendly.com/users/AAA",
        start_time="2026-01-01T00:00:00Z",
        end_time="2026-01-09T00:00:00Z",
    )
    assert called is False, "must reject before calling Calendly"
    assert result["error"]["code"] == "invalid_argument"


# ---------------------------------------------------------------------------
# TC-CAL-015 (AC10) -- handled by the middleware, covered in test_middleware.py
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# TC-CAL-016 (AC11)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_error_mapping_429_rate_limited(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(method: str, path: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        raise ToolError("rate_limited", "Calendly API returned HTTP 429", True)

    _install_fake_client(monkeypatch, handler)
    result = await _call("calendly_get_current_user")
    assert result == {"error": {"code": "rate_limited", "message": "Calendly API returned HTTP 429", "retryable": True}}


@pytest.mark.asyncio
async def test_error_mapping_500_upstream_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def handler(method: str, path: str, kwargs: dict[str, Any]) -> dict[str, Any]:
        raise ToolError("upstream_error", "Calendly API returned HTTP 500", True)

    _install_fake_client(monkeypatch, handler)
    result = await _call("calendly_get_current_user")
    assert result == {"error": {"code": "upstream_error", "message": "Calendly API returned HTTP 500", "retryable": True}}


@pytest.mark.asyncio
async def test_client_retries_and_maps_errors() -> None:
    """End-to-end (mocked transport) check of CalendlyClient's own status -> error mapping and retry/backoff."""
    attempts = {"count": 0}

    def handler_429_then_ok(request: httpx.Request) -> httpx.Response:
        attempts["count"] += 1
        if attempts["count"] == 1:
            return httpx.Response(429, headers={"Retry-After": "0"}, json={"title": "Too many requests", "message": "slow down"})
        return httpx.Response(200, json={"resource": {"uri": "u1"}})

    reset_token = _credential.set("test-token")
    try:
        async with CalendlyClient(transport=httpx.MockTransport(handler_429_then_ok)) as client:
            body = await client.request("GET", "/users/me")
        assert body == {"resource": {"uri": "u1"}}
        assert attempts["count"] == 2

        def handler_500(request: httpx.Request) -> httpx.Response:
            return httpx.Response(500, json={"title": "Internal Server Error", "message": "boom"})

        async with CalendlyClient(transport=httpx.MockTransport(handler_500)) as client:
            with pytest.raises(ToolError) as excinfo:
                await client.request("GET", "/users/me")
        assert excinfo.value.code == "upstream_error"
        assert excinfo.value.retryable is True

        def handler_404(request: httpx.Request) -> httpx.Response:
            return httpx.Response(404, json={"title": "Not Found", "message": "no such event"})

        async with CalendlyClient(transport=httpx.MockTransport(handler_404)) as client:
            with pytest.raises(ToolError) as excinfo:
                await client.request("GET", "/scheduled_events/does-not-exist")
        assert excinfo.value.code == "not_found"
        assert excinfo.value.retryable is False
    finally:
        _credential.reset(reset_token)


# ---------------------------------------------------------------------------
# TC-CAL-018 (AC13)
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_health_endpoint_ok_without_credentials() -> None:
    from calendly_mcp.server import build_app

    app = build_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/health")

    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}
