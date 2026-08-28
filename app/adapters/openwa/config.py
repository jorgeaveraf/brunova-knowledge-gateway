"""Environment-backed OpenWA MCP transport configuration."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlsplit


@dataclass(frozen=True, repr=False)
class OpenWAMCPConfig:
    url: str = field(repr=False)
    api_key: str = field(repr=False)
    transport: Literal["streamable_http"] = "streamable_http"

    @classmethod
    def from_environment(cls) -> "OpenWAMCPConfig":
        url = os.getenv("OPENWA_MCP_URL", "").strip()
        api_key = os.getenv("OPENWA_APIKEY", "").strip()
        missing = [
            name
            for name, value in (("OPENWA_MCP_URL", url), ("OPENWA_APIKEY", api_key))
            if not value
        ]
        if missing:
            raise ValueError(f"Missing OpenWA MCP configuration: {', '.join(missing)}")
        parsed = urlsplit(url)
        if parsed.scheme not in {"https", "http"} or not parsed.netloc:
            raise ValueError("OPENWA_MCP_URL is invalid")
        if parsed.query or parsed.fragment or parsed.path.rstrip("/") != "/mcp":
            raise ValueError("OPENWA_MCP_URL must identify the /mcp endpoint")
        return cls(url=url.rstrip("/"), api_key=api_key)

    @property
    def headers(self) -> dict[str, str]:
        return {"X-API-Key": self.api_key}

    def safe_summary(self) -> dict[str, Any]:
        return {
            "configured": True,
            "transport": self.transport,
            "has_endpoint": True,
            "authenticated": bool(self.api_key),
        }
