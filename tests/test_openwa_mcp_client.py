import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from app.adapters.google_workspace.errors import WorkspaceAdapterError
from app.adapters.openwa.config import OpenWAMCPConfig
from app.adapters.openwa.mcp_client import OpenWAMCPClient


class FakeResult:
    def __init__(self, value):
        self.value = value

    def model_dump(self, **_kwargs):
        return {"content": [{"type": "text", "text": self.value}], "isError": False}


class FakeDownstream:
    def __init__(self, tools):
        self.tools = tools
        self.calls = []
        self.server_info = SimpleNamespace(version="1.2.3")
        self.protocol_version = "2025-06-18"

    async def list_tools(self, **_kwargs):
        return SimpleNamespace(tools=self.tools, next_cursor=None)

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return FakeResult(name)


def descriptor(name, *, read_only):
    return SimpleNamespace(
        name=name,
        description=f"{name} description",
        input_schema={"type": "object"},
        meta={"category": "messages", "nested": {"authorization": "hidden"}},
        annotations=SimpleNamespace(
            model_dump=lambda **_kwargs: {
                "readOnlyHint": read_only,
                "destructiveHint": False,
            }
        ),
    )


def adapter(downstream, *, timeout=30):
    @asynccontextmanager
    async def factory():
        yield downstream

    return OpenWAMCPClient(
        OpenWAMCPConfig("https://wa.example/mcp", "secret"),
        client_factory=factory,
        timeout_seconds=timeout,
    )


def run(coro):
    return asyncio.run(coro)


def test_initialize_discovery_and_read_write_invocation():
    downstream = FakeDownstream([
        descriptor("MessageHistory", read_only=True),
        descriptor("MessageSendText", read_only=False),
    ])
    client = adapter(downstream)

    tools = run(client.list_tools())
    run(client.call_tool("MessageHistory", {"sessionId": "s1"}))
    run(client.call_tool("MessageSendText", {"sessionId": "s1", "text": "test"}))
    status = run(client.status())

    assert [tool.tier for tool in tools] == ["read", "write"]
    assert tools[0].metadata == {"category": "messages", "nested": {}}
    assert [call[0] for call in downstream.calls] == ["MessageHistory", "MessageSendText"]
    assert status.connected is True
    assert status.read_tool_count == 1
    assert status.write_tool_count == 1
    assert status.protocol_version == "2025-06-18"


def test_missing_tool_is_rejected_before_downstream_call():
    client = adapter(FakeDownstream([descriptor("MessageHistory", read_only=True)]))

    with pytest.raises(WorkspaceAdapterError) as raised:
        run(client.call_tool("Missing", {}))

    assert raised.value.code == "openwa_tool_unavailable"


def test_unavailable_and_timeout_errors_are_provider_scoped_and_sanitized():
    class Unavailable:
        async def list_tools(self, **_kwargs):
            raise RuntimeError("Authorization: Bearer top-secret message body")

    with pytest.raises(WorkspaceAdapterError) as unavailable:
        run(adapter(Unavailable()).list_tools())
    assert unavailable.value.code == "openwa_unavailable"
    assert "top-secret" not in str(unavailable.value)

    class Slow:
        async def list_tools(self, **_kwargs):
            await asyncio.sleep(0.05)

    with pytest.raises(WorkspaceAdapterError) as timeout:
        run(adapter(Slow(), timeout=0.001).list_tools())
    assert timeout.value.code == "openwa_timeout"


def test_authentication_failure_is_sanitized():
    class AuthError(RuntimeError):
        response = SimpleNamespace(status_code=401)

    class Rejected:
        async def list_tools(self, **_kwargs):
            raise AuthError("X-API-Key: top-secret")

    with pytest.raises(WorkspaceAdapterError) as raised:
        run(adapter(Rejected()).list_tools())

    assert raised.value.code == "openwa_authentication_failed"
    assert "top-secret" not in str(raised.value)
