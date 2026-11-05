"""Entry point: start the HTTP server (``python -m calendly_mcp``)."""

from __future__ import annotations

import uvicorn

from .server import build_app
from .settings import settings


def main() -> None:
    uvicorn.run(build_app(), host=settings.mcp_http_host, port=settings.mcp_http_port, log_level="info")


if __name__ == "__main__":
    main()
