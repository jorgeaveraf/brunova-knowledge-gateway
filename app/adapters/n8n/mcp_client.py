"""Official-SDK client for the n8n downstream MCP server."""

from __future__ import annotations

import time
import logging
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import anyio
import httpx2
from mcp import Client
from mcp.client.sse import sse_client
from mcp.client.stdio import StdioServerParameters, stdio_client
from mcp.client.streamable_http import streamable_http_client

from app.adapters.google_workspace.errors import WorkspaceAdapterError
from app.adapters.n8n.config import N8NMCPConfig
from app.adapters.n8n.models import N8NStatus, N8NToolDescriptor

# httpx2's INFO request line contains the downstream URL. Keep transport
# diagnostics at warning/error so secret-backed configuration never reaches logs.
logging.getLogger("httpx2").setLevel(logging.WARNING)
logging.getLogger("httpcore2").setLevel(logging.WARNING)


class N8NMCPClient:
    def __init__(
        self,
        config: N8NMCPConfig,
        *,
        discovery_ttl_seconds: int = 60,
        timeout_seconds: float = 30,
        client_factory: Callable[[], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.config = config
        self.discovery_ttl_seconds = discovery_ttl_seconds
        self.timeout_seconds = timeout_seconds
        self._client_factory = client_factory or self._default_client
        self._clock = clock
        self._tools: dict[str, N8NToolDescriptor] = {}
        self._expires_at = 0.0
        self._lock = anyio.Lock()
        self._protocol_version: str | None = None
        self._server_version: str | None = None
        self._initialized = False

    async def list_tools(self, *, force_refresh: bool = False) -> list[N8NToolDescriptor]:
        if not force_refresh and self._initialized and self._clock() < self._expires_at:
            return list(self._tools.values())
        async with self._lock:
            if not force_refresh and self._initialized and self._clock() < self._expires_at:
                return list(self._tools.values())
            try:
                async with self._client_factory() as client:
                    descriptors: list[N8NToolDescriptor] = []
                    cursor = None
                    while True:
                        with anyio.fail_after(self.timeout_seconds):
                            page = await client.list_tools(cursor=cursor, cache_mode="bypass")
                        for tool in page.tools:
                            metadata = getattr(tool, "meta", None) or {}
                            descriptors.append(N8NToolDescriptor(
                                name=tool.name,
                                description=tool.description,
                                input_schema=getattr(tool, "input_schema", {}) or {},
                                metadata=_safe_metadata(metadata),
                            ))
                        cursor = page.next_cursor
                        if not cursor:
                            break
                    self._capture_server_info(client)
            except TimeoutError as error:
                self.invalidate()
                raise WorkspaceAdapterError("n8n_timeout", "The n8n MCP request timed out.", 504) from error
            except WorkspaceAdapterError:
                self.invalidate()
                raise
            except Exception as error:
                self.invalidate()
                raise _adapter_error(error) from error
            self._tools = {tool.name: tool for tool in descriptors}
            self._initialized = True
            self._expires_at = self._clock() + self.discovery_ttl_seconds
            return list(self._tools.values())

    async def call_tool(self, tool_name: str, arguments: dict[str, Any]) -> Any:
        available = {tool.name for tool in await self.list_tools()}
        if tool_name not in available:
            raise WorkspaceAdapterError("n8n_tool_unavailable", "The n8n tool is not currently exposed.", 404)
        try:
            async with self._client_factory() as client:
                with anyio.fail_after(self.timeout_seconds):
                    result = await client.call_tool(tool_name, arguments)
                self._capture_server_info(client)
                return result.model_dump(mode="json", by_alias=True) if hasattr(result, "model_dump") else result
        except TimeoutError as error:
            raise WorkspaceAdapterError("n8n_timeout", "The n8n MCP request timed out.", 504) from error
        except WorkspaceAdapterError:
            raise
        except Exception as error:
            self.invalidate()
            # Re-discovery makes removals visible immediately after a downstream error.
            try:
                await self.list_tools(force_refresh=True)
            except WorkspaceAdapterError:
                pass
            if tool_name not in self._tools:
                raise WorkspaceAdapterError("n8n_tool_unavailable", "The n8n tool is no longer exposed.", 404) from error
            mapped = _adapter_error(error)
            if mapped.code == "n8n_unavailable":
                mapped = WorkspaceAdapterError("n8n_downstream_error", "The n8n MCP tool failed.", 502)
            raise mapped from error

    async def status(self) -> N8NStatus:
        try:
            tools = await self.list_tools(force_refresh=True)
            return N8NStatus(configured=True, connected=True, mcp_initialized=True,
                tool_count=len(tools), protocol_version=self._protocol_version,
                server_version=self._server_version)
        except WorkspaceAdapterError:
            return N8NStatus(configured=True, connected=False, mcp_initialized=False, tool_count=0)

    def invalidate(self) -> None:
        self._tools = {}
        self._expires_at = 0.0
        self._initialized = False

    def _capture_server_info(self, client: Any) -> None:
        info = getattr(client, "server_info", None)
        version = getattr(info, "version", None) if info else None
        self._server_version = str(version) if version else self._server_version
        protocol = getattr(client, "protocol_version", None)
        self._protocol_version = str(protocol) if protocol else self._protocol_version

    @asynccontextmanager
    async def _default_client(self) -> AsyncIterator[Client]:
        if self.config.transport == "streamable_http":
            async with httpx2.AsyncClient(
                headers=self.config.headers,
                follow_redirects=True,
                timeout=self.timeout_seconds,
            ) as http_client:
                async with Client(streamable_http_client(self.config.url, http_client=http_client), mode="legacy") as client:
                    yield client
            return
        if self.config.transport == "sse":
            async with Client(sse_client(self.config.url, headers=self.config.headers), mode="legacy") as client:
                yield client
            return
        parameters = StdioServerParameters(command=self.config.command, args=list(self.config.args), env=self.config.environment or None)
        async with Client(stdio_client(parameters), mode="legacy") as client:
            yield client


def _safe_metadata(metadata: Any) -> dict[str, Any]:
    if not isinstance(metadata, dict):
        return {}
    blocked = ("authorization", "token", "secret", "password", "credential", "header", "cookie", "api_key", "apikey")

    def scrub(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                key: scrub(child)
                for key, child in value.items()
                if not any(fragment in key.casefold() for fragment in blocked)
            }
        if isinstance(value, list):
            return [scrub(child) for child in value]
        return value

    return scrub(metadata)


def _adapter_error(error: BaseException) -> WorkspaceAdapterError:
    pending: list[BaseException] = [error]
    while pending:
        current = pending.pop()
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
        status = getattr(getattr(current, "response", None), "status_code", None)
        if status in {401, 403}:
            return WorkspaceAdapterError(
                "n8n_authentication_failed",
                "The n8n MCP credentials were rejected.",
                503,
            )
        cause = current.__cause__ or current.__context__
        if cause is not None and cause is not current:
            pending.append(cause)
    return WorkspaceAdapterError("n8n_unavailable", "The n8n MCP server is unavailable.", 503)
