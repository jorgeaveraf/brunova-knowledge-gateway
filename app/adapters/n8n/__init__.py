"""n8n downstream MCP adapter."""

from app.adapters.n8n.config import N8NMCPConfig
from app.adapters.n8n.mcp_client import N8NMCPClient

__all__ = ["N8NMCPClient", "N8NMCPConfig"]
