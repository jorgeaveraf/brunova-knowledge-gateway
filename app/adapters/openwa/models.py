"""Safe models exposed by the OpenWA MCP boundary."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class OpenWAToolDescriptor(BaseModel):
    name: str
    description: str | None = None
    input_schema: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    annotations: dict[str, Any] = Field(default_factory=dict)
    tier: Literal["read", "write"]


class OpenWAToolListResult(BaseModel):
    tools: list[OpenWAToolDescriptor]
    request_id: str


class OpenWAStatus(BaseModel):
    provider: Literal["openwa"] = "openwa"
    configured: bool
    connected: bool
    mcp_initialized: bool
    tool_count: int
    read_tool_count: int = 0
    write_tool_count: int = 0
    protocol_version: str | None = None
    server_version: str | None = None


class OpenWAStatusResult(OpenWAStatus):
    request_id: str
