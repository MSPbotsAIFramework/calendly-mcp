"""FastMCP factory and the outer Starlette app (PRD-21349).

Wires together: the stateless Streamable HTTP MCP endpoint, the
request-scoped credential middleware, and a pure local ``/health``
endpoint that never calls out to Calendly (vendor-mcp SOP S1.1, S7).
"""

from __future__ import annotations

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from . import tools
from .middleware import CredentialMiddleware
from .settings import settings

INSTRUCTIONS = (
    "Calendly scheduling connector. Lets an agent read the connected account's "
    "meeting types (event types), check open availability and busy times, list "
    "and inspect scheduled meetings and their invitees, cancel a meeting, and "
    "generate a single-use scheduling link to share with someone else. "
    "Typical flow: calendly_get_current_user to get the account's own URIs, "
    "then calendly_list_event_types to find a meeting type, then "
    "calendly_get_event_type_available_times or calendly_create_scheduling_link "
    "to help arrange a time. This connector is read-mostly: the only write "
    "actions are canceling an existing meeting and creating a scheduling link; "
    "it never books a meeting on an invitee's behalf."
)


def build_mcp() -> FastMCP:
    mcp = FastMCP(
        "calendly-mcp",
        instructions=INSTRUCTIONS,
        stateless_http=True,
        host=settings.mcp_http_host,
        port=settings.mcp_http_port,
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    tools.register(mcp)
    return mcp


async def _health(_request: Request) -> JSONResponse:
    return JSONResponse({"status": "ok"})


def build_app(mcp: FastMCP | None = None) -> Starlette:
    """Build the outer Starlette app: /health + /mcp, one shared lifespan.

    ``Mount()`` alone would not run the mounted app's lifespan (the MCP
    session manager would never start, and every /mcp call would fail with
    "Task group is not initialized") -- the inner app's lifespan is passed
    through to the outer app explicitly.
    """
    mcp = mcp or build_mcp()
    mcp_app = mcp.streamable_http_app()

    return Starlette(
        routes=[
            Route("/health", _health, methods=["GET"]),
            Mount("/", app=mcp_app),
        ],
        middleware=[Middleware(CredentialMiddleware)],
        lifespan=mcp_app.router.lifespan_context,
    )
