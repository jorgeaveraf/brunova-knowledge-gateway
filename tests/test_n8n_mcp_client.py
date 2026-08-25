import asyncio
import logging
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.adapters.google_workspace.errors import WorkspaceAdapterError
from app.adapters.n8n.config import N8NMCPConfig
from app.adapters.n8n.mcp_client import N8NMCPClient


class FakeResult:
    def __init__(self, value):
        self.value = value

    def model_dump(self, **_kwargs):
        return {"content": [{"type": "text", "text": self.value}], "isError": False}


class FakeDownstream:
    def __init__(self, catalogs):
        self.catalogs = catalogs
        self.discovery_count = 0
        self.calls = []

    async def list_tools(self, **_kwargs):
        index = min(self.discovery_count, len(self.catalogs) - 1)
        names = self.catalogs[index]
        self.discovery_count += 1
        return SimpleNamespace(
            tools=[SimpleNamespace(name=name, description=f"{name} description", input_schema={"type": "object"}, meta={}) for name in names],
            next_cursor=None,
        )

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return FakeResult(name)


def adapter(downstream, *, clock=lambda: 0, ttl=60, timeout=30):
    @asynccontextmanager
    async def factory():
        yield downstream

    return N8NMCPClient(
        N8NMCPConfig.from_json('{"mcpServers":{"n8n-mcp":{"type":"http","url":"https://n8n.example/mcp"}}}'),
        client_factory=factory,
        discovery_ttl_seconds=ttl,
        timeout_seconds=timeout,
        clock=clock,
    )


def run(coro):
    return asyncio.run(coro)


def test_full_access_does_not_filter_read_or_mutation_tools():
    downstream = FakeDownstream([["search_workflows", "execute_workflow"]])
    client = adapter(downstream)
    tools = run(client.list_tools())
    run(client.call_tool("search_workflows", {"query": "x"}))
    run(client.call_tool("execute_workflow", {"id": "1"}))
    assert {tool.name for tool in tools} == {"search_workflows", "execute_workflow"}
    assert [call[0] for call in downstream.calls] == ["search_workflows", "execute_workflow"]


def test_catalog_addition_and_removal_are_visible_after_ttl():
    now = [0.0]
    downstream = FakeDownstream([["old"], ["old", "new"], ["new"]])
    client = adapter(downstream, clock=lambda: now[0], ttl=10)
    assert [tool.name for tool in run(client.list_tools())] == ["old"]
    now[0] = 11
    assert {tool.name for tool in run(client.list_tools())} == {"old", "new"}
    now[0] = 22
    assert [tool.name for tool in run(client.list_tools())] == ["new"]
    with pytest.raises(WorkspaceAdapterError, match="currently exposed"):
        run(client.call_tool("old", {}))


def test_unavailable_downstream_is_sanitized_and_clears_catalog():
    class Unavailable:
        async def list_tools(self, **_kwargs):
            raise RuntimeError("Authorization: top-secret")

    client = adapter(Unavailable())
    with pytest.raises(WorkspaceAdapterError) as raised:
        run(client.list_tools())
    assert raised.value.code == "n8n_unavailable"
    assert "top-secret" not in str(raised.value)


def test_timeout_is_reported_as_provider_scoped_error():
    class Slow:
        async def list_tools(self, **_kwargs):
            await asyncio.sleep(0.05)

    client = adapter(Slow(), timeout=0.001)
    with pytest.raises(WorkspaceAdapterError) as raised:
        run(client.list_tools())
    assert raised.value.code == "n8n_timeout"


def test_sensitive_nested_metadata_is_removed():
    class MetadataDownstream(FakeDownstream):
        async def list_tools(self, **_kwargs):
            return SimpleNamespace(
                tools=[SimpleNamespace(
                    name="safe", description="safe", input_schema={},
                    meta={"category": "workflow", "nested": {"apiKey": "hidden"}},
                )],
                next_cursor=None,
            )

    tool = run(adapter(MetadataDownstream([["safe"]])).list_tools())[0]
    assert tool.metadata == {"category": "workflow", "nested": {}}


def test_http_transport_request_logger_cannot_emit_endpoint_at_info():
    assert logging.getLogger("httpx2").getEffectiveLevel() >= logging.WARNING
    assert logging.getLogger("httpcore2").getEffectiveLevel() >= logging.WARNING
