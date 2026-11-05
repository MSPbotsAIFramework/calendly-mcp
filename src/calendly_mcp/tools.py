"""The 9 Calendly tools (PRD-21349 tech design S "Data & API contract").

Each tool is a thin 1:1 wrapper around one Calendly REST endpoint: validate
input, call the API via ``CalendlyClient``, shape the response to the fields
an Agent needs, serialize compactly. Every tool is wrapped by ``_guarded``
so a ``ToolError`` -- or any unexpected exception -- always comes back as
the structured error envelope (SOP S4), never a raised exception.

Tool-level ``description=`` strings are passed explicitly (not left to
docstring extraction): FastMCP uses the raw ``__doc__`` verbatim, including
indentation, which both bloats the token-costed description (SOP S2.2) and
looks unpolished to the Agent. Per-parameter guidance instead lives on each
parameter via ``Annotated[..., Field(description=...)]`` (SOP S2.4), which
FastMCP folds into the generated JSON Schema.
"""

from __future__ import annotations

import functools
from datetime import datetime, timedelta
from typing import Annotated, Any, Awaitable, Callable, TypeVar

from mcp.server.fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import Field

from .client import CalendlyClient
from .errors import ToolError, dumps

MAX_RESPONSE_CHARS = 20_000
DEFAULT_COUNT = 20
MAX_COUNT = 100

_F = TypeVar("_F", bound=Callable[..., Awaitable[str]])


def _guarded(fn: _F) -> _F:
    @functools.wraps(fn)
    async def wrapper(*args: Any, **kwargs: Any) -> str:
        try:
            return await fn(*args, **kwargs)
        except ToolError as exc:
            return exc.to_json()
        except Exception as exc:  # pragma: no cover -- last-resort safety net, SOP S4 "never raise"
            return ToolError("upstream_error", f"Unexpected error calling Calendly: {exc}", True).to_json()

    return wrapper  # type: ignore[return-value]


def _clamp_count(count: int) -> int:
    return max(1, min(count, MAX_COUNT))


def _parse_time(value: str, field: str) -> datetime:
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ToolError("invalid_argument", f"{field} is not a valid ISO-8601 timestamp: {value}", False) from exc


def _require_window(start_time: str, end_time: str, max_days: int, tool_name: str) -> None:
    start = _parse_time(start_time, "start_time")
    end = _parse_time(end_time, "end_time")
    if end <= start:
        raise ToolError("invalid_argument", "end_time must be after start_time", False)
    if end - start > timedelta(days=max_days):
        raise ToolError(
            "invalid_argument",
            f"{tool_name} window must be {max_days} days or less (Calendly's documented limit)",
            False,
        )


def _finish(collection_key: str, payload: dict[str, Any]) -> str:
    """Serialize, enforcing the SOP S2.5 character budget by trimming the collection."""
    text = dumps(payload)
    if len(text) <= MAX_RESPONSE_CHARS or collection_key not in payload:
        return text

    items = list(payload[collection_key])
    while items and len(text) > MAX_RESPONSE_CHARS:
        items.pop()
        payload = {**payload, collection_key: items, "has_more": True}
        text = dumps(payload)
    return text


# Reusable parameter annotations shared by more than one tool.
_Count = Annotated[int, Field(description="Max rows to return. Default 20, hard cap 100.")]
_PageToken = Annotated[
    str | None, Field(default=None, description="Pagination token from a previous call's next_page_token.")
]
_EventUuid = Annotated[str, Field(description="The event's UUID, as returned by calendly_list_scheduled_events.")]


def register(mcp: FastMCP) -> None:
    """Register all 9 Calendly tools on the given FastMCP instance."""

    @mcp.tool(
        description="Get the connected Calendly account's own identity.\n"
        "Returns name, email, scheduling URL, and the user/organization URIs other tools take as input.",
        annotations=ToolAnnotations(readOnlyHint=True),
        structured_output=False,
    )
    @_guarded
    async def calendly_get_current_user() -> str:
        async with CalendlyClient() as client:
            body = await client.request("GET", "/users/me")
        resource = body.get("resource", {})
        shaped = {
            "uri": resource.get("uri"),
            "name": resource.get("name"),
            "email": resource.get("email"),
            "scheduling_url": resource.get("scheduling_url"),
            "current_organization": resource.get("current_organization"),
        }
        return dumps(shaped)

    @mcp.tool(
        description="List the account's Calendly event types (meeting types).\n"
        "Each has the scheduling URL invitees use to book it.",
        annotations=ToolAnnotations(readOnlyHint=True),
        structured_output=False,
    )
    @_guarded
    async def calendly_list_event_types(
        active: Annotated[bool | None, Field(default=None, description="Filter to active (true) or inactive (false) event types.")] = None,
        organization: Annotated[
            str | None, Field(default=None, description="Organization URI to scope the list to (admin/owner accounts only).")
        ] = None,
        user: Annotated[str | None, Field(default=None, description="User URI to scope the list to.")] = None,
        count: _Count = DEFAULT_COUNT,
        page_token: _PageToken = None,
    ) -> str:
        params: dict[str, Any] = {"count": _clamp_count(count)}
        if active is not None:
            params["active"] = active
        if organization:
            params["organization"] = organization
        if user:
            params["user"] = user
        if page_token:
            params["page_token"] = page_token

        async with CalendlyClient() as client:
            body = await client.request("GET", "/event_types", params=params)

        collection = [
            {
                "uri": item.get("uri"),
                "name": item.get("name"),
                "duration": item.get("duration"),
                "scheduling_url": item.get("scheduling_url"),
                "kind": item.get("kind"),
                "active": item.get("active"),
            }
            for item in body.get("collection", [])
        ]
        pagination = body.get("pagination") or {}
        payload = {"collection": collection, "has_more": bool(pagination.get("next_page_token"))}
        return _finish("collection", payload)

    @mcp.tool(
        description="List open booking slots for one event type in a date range.\n"
        "Window must be 31 days or less (Calendly's own limit).",
        annotations=ToolAnnotations(readOnlyHint=True),
        structured_output=False,
    )
    @_guarded
    async def calendly_get_event_type_available_times(
        event_type: Annotated[str, Field(description="The event type's URI, from calendly_list_event_types.")],
        start_time: Annotated[str, Field(description='Range start, ISO-8601 (e.g. "2026-01-01T00:00:00Z").')],
        end_time: Annotated[str, Field(description="Range end, ISO-8601. Must be within 31 days of start_time.")],
    ) -> str:
        _require_window(start_time, end_time, 31, "calendly_get_event_type_available_times")

        async with CalendlyClient() as client:
            body = await client.request(
                "GET",
                "/event_type_available_times",
                params={"event_type": event_type, "start_time": start_time, "end_time": end_time},
            )

        collection = [
            {
                "status": item.get("status"),
                "invitees_remaining": item.get("invitees_remaining"),
                "start_time": item.get("start_time"),
                "scheduling_url": item.get("scheduling_url"),
            }
            for item in body.get("collection", [])
        ]
        return _finish("collection", {"collection": collection})

    @mcp.tool(
        description="List the account's scheduled meetings (events), newest first by default.",
        annotations=ToolAnnotations(readOnlyHint=True),
        structured_output=False,
    )
    @_guarded
    async def calendly_list_scheduled_events(
        user: Annotated[str | None, Field(default=None, description="User URI to scope the list to.")] = None,
        organization: Annotated[
            str | None, Field(default=None, description="Organization URI to scope the list to (admin/owner accounts only).")
        ] = None,
        invitee_email: Annotated[
            str | None, Field(default=None, description="Only return events with this invitee email.")
        ] = None,
        status: Annotated[str | None, Field(default=None, description='Filter by "active" or "canceled".')] = None,
        min_start_time: Annotated[
            str | None, Field(default=None, description="Only events starting at/after this ISO-8601 time.")
        ] = None,
        max_start_time: Annotated[
            str | None, Field(default=None, description="Only events starting before this ISO-8601 time.")
        ] = None,
        count: _Count = DEFAULT_COUNT,
        page_token: _PageToken = None,
    ) -> str:
        params: dict[str, Any] = {"count": _clamp_count(count), "sort": "start_time:desc"}
        if user:
            params["user"] = user
        if organization:
            params["organization"] = organization
        if invitee_email:
            params["invitee_email"] = invitee_email
        if status:
            params["status"] = status
        if min_start_time:
            params["min_start_time"] = min_start_time
        if max_start_time:
            params["max_start_time"] = max_start_time
        if page_token:
            params["page_token"] = page_token

        async with CalendlyClient() as client:
            body = await client.request("GET", "/scheduled_events", params=params)

        collection = [
            {
                "uri": item.get("uri"),
                "name": item.get("name"),
                "start_time": item.get("start_time"),
                "end_time": item.get("end_time"),
                "status": item.get("status"),
                "event_type": item.get("event_type"),
                "location": item.get("location"),
            }
            for item in body.get("collection", [])
        ]
        pagination = body.get("pagination") or {}
        payload = {"collection": collection, "has_more": bool(pagination.get("next_page_token"))}
        return _finish("collection", payload)

    @mcp.tool(
        description="Get full detail for one of the account's scheduled events.\n"
        "Includes time, location, status, event type, and invitee count.",
        annotations=ToolAnnotations(readOnlyHint=True),
        structured_output=False,
    )
    @_guarded
    async def calendly_get_scheduled_event(uuid: _EventUuid) -> str:
        async with CalendlyClient() as client:
            body = await client.request("GET", f"/scheduled_events/{uuid}")

        resource = body.get("resource", {})
        counter = resource.get("invitees_counter") or {}
        shaped = {
            "uri": resource.get("uri"),
            "name": resource.get("name"),
            "status": resource.get("status"),
            "start_time": resource.get("start_time"),
            "end_time": resource.get("end_time"),
            "event_type": resource.get("event_type"),
            "location": resource.get("location"),
            "invitees_counter": {
                "total": counter.get("total"),
                "active": counter.get("active"),
                "limit": counter.get("limit"),
            },
        }
        cancellation = resource.get("cancellation")
        if cancellation:
            shaped["cancellation"] = {
                "canceled_by": cancellation.get("canceled_by"),
                "reason": cancellation.get("reason"),
            }
        return dumps(shaped)

    @mcp.tool(
        description="List who is invited to one scheduled event.\n"
        "Includes name, email, status, and cancellation reason if canceled.",
        annotations=ToolAnnotations(readOnlyHint=True),
        structured_output=False,
    )
    @_guarded
    async def calendly_list_event_invitees(
        uuid: _EventUuid,
        status: Annotated[str | None, Field(default=None, description='Filter by "active" or "canceled".')] = None,
        email: Annotated[str | None, Field(default=None, description="Only return the invitee with this email.")] = None,
        count: _Count = DEFAULT_COUNT,
        page_token: _PageToken = None,
    ) -> str:
        params: dict[str, Any] = {"count": _clamp_count(count)}
        if status:
            params["status"] = status
        if email:
            params["email"] = email
        if page_token:
            params["page_token"] = page_token

        async with CalendlyClient() as client:
            body = await client.request("GET", f"/scheduled_events/{uuid}/invitees", params=params)

        collection = []
        for item in body.get("collection", []):
            cancellation = item.get("cancellation")
            collection.append(
                {
                    "name": item.get("name"),
                    "email": item.get("email"),
                    "status": item.get("status"),
                    "cancellation_reason": cancellation.get("reason") if cancellation else None,
                }
            )
        pagination = body.get("pagination") or {}
        payload = {"collection": collection, "has_more": bool(pagination.get("next_page_token"))}
        return _finish("collection", payload)

    @mcp.tool(
        description="Cancel one of the account's scheduled events. Irreversible.\n"
        "Only accepts a single explicit event UUID, never a filter or a batch.",
        annotations=ToolAnnotations(destructiveHint=True, idempotentHint=True),
        structured_output=False,
    )
    @_guarded
    async def calendly_cancel_scheduled_event(
        uuid: _EventUuid,
        reason: Annotated[
            str | None, Field(default=None, description="Optional cancellation reason shown to the invitee(s).")
        ] = None,
    ) -> str:
        if reason is not None and len(reason) > 10_000:
            raise ToolError("invalid_argument", "reason must be 10000 characters or fewer", False)

        body: dict[str, Any] = {"reason": reason} if reason else {}
        async with CalendlyClient() as client:
            await client.request("POST", f"/scheduled_events/{uuid}/cancellation", json=body)

        return dumps({"uuid": uuid, "status": "canceled"})

    @mcp.tool(
        description="List a user's busy blocks: internal Calendly events plus external calendar conflicts.\n"
        "Window must be 7 days or less (Calendly's own limit).",
        annotations=ToolAnnotations(readOnlyHint=True),
        structured_output=False,
    )
    @_guarded
    async def calendly_list_user_busy_times(
        user: Annotated[str, Field(description="The user's URI, from calendly_get_current_user.")],
        start_time: Annotated[str, Field(description="Range start, ISO-8601. Cannot be in the past.")],
        end_time: Annotated[str, Field(description="Range end, ISO-8601. Must be within 7 days of start_time.")],
    ) -> str:
        _require_window(start_time, end_time, 7, "calendly_list_user_busy_times")

        async with CalendlyClient() as client:
            body = await client.request(
                "GET",
                "/user_busy_times",
                params={"user": user, "start_time": start_time, "end_time": end_time},
            )

        collection = [
            {
                "type": item.get("type"),
                "start_time": item.get("start_time"),
                "end_time": item.get("end_time"),
            }
            for item in body.get("collection", [])
        ]
        return _finish("collection", {"collection": collection})

    @mcp.tool(
        description="Create a single-use scheduling link for an event type, to share with someone else.\n"
        "They pick their own time; the agent never books on an invitee's behalf.",
        annotations=ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=False),
        structured_output=False,
    )
    @_guarded
    async def calendly_create_scheduling_link(
        owner: Annotated[str, Field(description="The event type's URI, from calendly_list_event_types.")],
        max_event_count: Annotated[
            int, Field(description="Must be 1 -- Calendly currently only supports single-use links.")
        ] = 1,
    ) -> str:
        if max_event_count != 1:
            raise ToolError(
                "invalid_argument",
                "max_event_count must be 1 -- Calendly's API only supports single-use scheduling links",
                False,
            )

        async with CalendlyClient() as client:
            body = await client.request(
                "POST",
                "/scheduling_links",
                json={"max_event_count": "1", "owner": owner, "owner_type": "EventType"},
            )

        resource = body.get("resource", {})
        shaped = {
            "booking_url": resource.get("booking_url"),
            "owner": resource.get("owner"),
            "owner_type": resource.get("owner_type"),
        }
        return dumps(shaped)
