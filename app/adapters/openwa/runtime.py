"""Lazy OpenWA runtime isolated from other provider runtimes."""

from functools import lru_cache

from app.adapters.openwa.config import OpenWAMCPConfig
from app.adapters.openwa.mcp_client import OpenWAMCPClient
from app.config.settings import get_settings


@lru_cache
def get_openwa_client() -> OpenWAMCPClient:
    settings = get_settings()
    return OpenWAMCPClient(
        OpenWAMCPConfig.from_environment(),
        discovery_ttl_seconds=settings.openwa_discovery_ttl_seconds,
        timeout_seconds=settings.openwa_timeout_seconds,
    )
