"""Official-SDK client for the OpenWA downstream MCP server."""

from __future__ import annotations

import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

import anyio
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from app.adapters.google_workspace.errors import WorkspaceAdapterError
from app.adapters.openwa.config import OpenWAMCPConfig
from app.adapters.openwa.models import OpenWAStatus, OpenWAToolDescriptor

# Request-line logging can disclose downstream topology, while exceptions may
# contain headers. Public errors below are always provider-scoped and sanitized.
logging.getLogger("httpx2").setLevel(logging.WARNING)
logging.getLogger("httpcore2").setLevel(logging.WARNING)


class OpenWAMCPClient:
    def __init__(
        self,
        config: OpenWAMCPConfig,
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
        self._tools: dict[str, OpenWAToolDescriptor] = {}
        self._expires_at = 0.0
        self._lock = anyio.Lock()
        self._protocol_version: str | None = None
        self._server_version: str | None = None
        self._initialized = False

    async def list_tools(self, *, force_refresh: bool = False) -> list[OpenWAToolDescriptor]:
        if not force_refresh and self._initialized and self._clock() < self._expires_at:
            return list(self._tools.values())
        async with self._lock:
            if not force_refresh and self._initialized and self._clock() < self._expires_at:
                return list(self._tools.values())
            try:
                async with self._client_factory() as client:
                    descriptors: list[OpenWAToolDescriptor] = []
                    cursor = None
                    while True:
                        with anyio.fail_after(self.timeout_seconds):
                            page = await client.list_tools(cursor=cursor, cache_mode="bypass")
                        for tool in page.tools:
                            annotations = _model_mapping(getattr(tool, "annotations", None))
                            descriptors.append(
                                OpenWAToolDescriptor(
                                    name=tool.name,
                                    description=tool.description,
                                    input_schema=getattr(tool, "input_schema", {}) or {},
                                    metadata=_safe_mapping(getattr(tool, "meta", None)),
                                    annotations=_safe_mapping(annotations),
                                    tier="read" if annotations.get("readOnlyHint") is True else "write",
                                )
                            )
                        cursor = page.next_cursor
                        if not cursor:
                            break
                    self._capture_server_info(client)
            except TimeoutError as error:
                self.invalidate()
                raise WorkspaceAdapterError(
                    "openwa_timeout", "The OpenWA MCP request timed out.", 504
                ) from error
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
            raise WorkspaceAdapterError(
                "openwa_tool_unavailable", "The OpenWA tool is not currently exposed.", 404
            )
        try:
            async with self._client_factory() as client:
                with anyio.fail_after(self.timeout_seconds):
                    result = await client.call_tool(tool_name, arguments)
                self._capture_server_info(client)
                return result.model_dump(mode="json", by_alias=True) if hasattr(result, "model_dump") else result
        except TimeoutError as error:
            raise WorkspaceAdapterError(
                "openwa_timeout", "The OpenWA MCP request timed out.", 504
            ) from error
        except WorkspaceAdapterError:
            raise
        except Exception as error:
            self.invalidate()
            try:
                await self.list_tools(force_refresh=True)
            except WorkspaceAdapterError:
                pass
            if tool_name not in self._tools:
                raise WorkspaceAdapterError(
                    "openwa_tool_unavailable", "The OpenWA tool is no longer exposed.", 404
                ) from error
            mapped = _adapter_error(error)
            if mapped.code == "openwa_unavailable":
                mapped = WorkspaceAdapterError(
                    "openwa_downstream_error", "The OpenWA MCP tool failed.", 502
                )
            raise mapped from error

    async def status(self) -> OpenWAStatus:
        try:
            tools = await self.list_tools(force_refresh=True)
            return OpenWAStatus(
                configured=True,
                connected=True,
                mcp_initialized=True,
                tool_count=len(tools),
                read_tool_count=sum(tool.tier == "read" for tool in tools),
                write_tool_count=sum(tool.tier == "write" for tool in tools),
                protocol_version=self._protocol_version,
                server_version=self._server_version,
            )
        except WorkspaceAdapterError:
            return OpenWAStatus(
                configured=True, connected=False, mcp_initialized=False, tool_count=0
            )

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
        async with httpx2.AsyncClient(
            headers=self.config.headers,
            follow_redirects=True,
            timeout=self.timeout_seconds,
        ) as http_client:
            async with Client(
                streamable_http_client(self.config.url, http_client=http_client),
                mode="legacy",
            ) as client:
                yield client


def _model_mapping(value: Any) -> dict[str, Any]:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json", by_alias=True, exclude_none=True)
    return value if isinstance(value, dict) else {}


def _safe_mapping(value: Any) -> dict[str, Any]:
    value = _model_mapping(value)
    blocked = (
        "authorization", "token", "secret", "password", "credential",
        "header", "cookie", "api_key", "apikey",
    )

    def scrub(item: Any) -> Any:
        if isinstance(item, dict):
            return {
                key: scrub(child)
                for key, child in item.items()
                if not any(fragment in key.casefold() for fragment in blocked)
            }
        if isinstance(item, list):
            return [scrub(child) for child in item]
        return item

    return scrub(value)


def _adapter_error(error: BaseException) -> WorkspaceAdapterError:
    pending: list[BaseException] = [error]
    while pending:
        current = pending.pop()
        if isinstance(current, BaseExceptionGroup):
            pending.extend(current.exceptions)
        status = getattr(getattr(current, "response", None), "status_code", None)
        if status in {401, 403}:
            return WorkspaceAdapterError(
                "openwa_authentication_failed",
                "The OpenWA MCP credentials were rejected.",
                503,
            )
        cause = current.__cause__ or current.__context__
        if cause is not None and cause is not current:
            pending.append(cause)
    return WorkspaceAdapterError(
        "openwa_unavailable", "The OpenWA MCP server is unavailable.", 503
    )
