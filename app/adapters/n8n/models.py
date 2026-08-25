"""Safe models exposed by the n8n MCP boundary."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class N8NToolDescriptor(BaseModel):
    name: str
    description: str | None = None
    input_schema: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


class N8NToolListResult(BaseModel):
    tools: list[N8NToolDescriptor]
    request_id: str


class N8NStatus(BaseModel):
    provider: Literal["n8n"] = "n8n"
    configured: bool
    connected: bool
    mcp_initialized: bool
    tool_count: int
    protocol_version: str | None = None
    server_version: str | None = None


class N8NStatusResult(N8NStatus):
    request_id: str


class N8NToolResult(BaseModel):
    provider: Literal["n8n"] = "n8n"
    tool: str
    result: Any
    request_id: str
