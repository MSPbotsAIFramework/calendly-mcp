"""Non-credential runtime configuration.

Per the vendor-mcp SOP (S1.2 / S3.1), credentials are never read from
environment variables or config files -- they arrive per-request in an
HTTP header (see middleware.py). This module only holds listen address
and the Calendly base URL.
"""

from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration, tolerant of unknown environment variables.

    ``extra="ignore"`` is required: the deployment environment injects
    variables this project never declared, and without it the process
    would crash on startup (SOP S1.2).
    """

    model_config = SettingsConfigDict(extra="ignore")

    mcp_http_host: str = "0.0.0.0"
    mcp_http_port: int = 8080
    calendly_base_url: str = "https://api.calendly.com"


settings = Settings()
