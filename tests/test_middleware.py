"""Unit tests for the request-scoped credential middleware (vendor-mcp SOP S3).

TC-CAL-015 (AC10): missing header -> 401, naming the header, no downstream call.
TC-CAL-017 (AC12): two concurrent requests with different tokens never cross.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from calendly_mcp.middleware import CREDENTIAL_HEADER, CredentialMiddleware, get_credential


def _make_echo_app() -> Starlette:
    async def echo(_request: Request) -> JSONResponse:
        # Yield control so concurrent requests actually interleave.
        await asyncio.sleep(0)
        return JSONResponse({"token": get_credential()})

    return Starlette(
        routes=[Route("/mcp", echo, methods=["POST"])],
        middleware=[Middleware(CredentialMiddleware)],
    )


@pytest.mark.asyncio
async def test_missing_header_returns_401_no_upstream_call() -> None:
    called = False

    async def downstream(scope, receive, send):  # noqa: ANN001 -- raw ASGI signature
        nonlocal called
        called = True

    app = CredentialMiddleware(downstream)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/mcp", json={})

    assert resp.status_code == 401
    body = resp.json()
    assert body["error"]["code"] == "unauthorized"
    assert CREDENTIAL_HEADER in body["error"]["message"]
    assert called is False, "must not call the downstream app when the credential header is missing"


@pytest.mark.asyncio
async def test_header_present_reaches_contextvar() -> None:
    app = _make_echo_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/mcp", json={}, headers={CREDENTIAL_HEADER: "token-a"})

    assert resp.status_code == 200
    assert resp.json()["token"] == "token-a"


@pytest.mark.asyncio
async def test_concurrent_requests_do_not_cross_tokens() -> None:
    app = _make_echo_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        resp_a, resp_b = await asyncio.gather(
            client.post("/mcp", json={}, headers={CREDENTIAL_HEADER: "token-a"}),
            client.post("/mcp", json={}, headers={CREDENTIAL_HEADER: "token-b"}),
        )

    assert resp_a.json()["token"] == "token-a"
    assert resp_b.json()["token"] == "token-b"
