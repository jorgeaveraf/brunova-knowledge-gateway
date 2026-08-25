import asyncio

from mcp import Client

import app.mcp_server as mcp_module
from app.adapters.n8n.models import N8NToolDescriptor


class FakeN8NClient:
    def __init__(self):
        self.calls = []

    async def list_tools(self, **_kwargs):
        return [
            N8NToolDescriptor(
                name="read_capability",
                description="read",
                input_schema={"type": "object", "properties": {"query": {"type": "string"}}, "required": ["query"]},
            ),
            N8NToolDescriptor(
                name="mutation_capability",
                description="mutation",
                input_schema={"type": "object", "properties": {"value": {"type": "integer"}}, "required": ["value"]},
            ),
        ]

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return {"content": [{"type": "text", "text": "ok"}], "isError": False}


def test_gateway_projects_exact_schemas_and_all_downstream_tools(monkeypatch):
    fake = FakeN8NClient()
    monkeypatch.setattr(mcp_module, "get_n8n_client", lambda: fake)

    async def scenario():
        async with Client(mcp_module.mcp_server) as client:
            catalog = await client.list_tools()
            await client.call_tool("n8n_read_capability", {"query": "x"})
            await client.call_tool("n8n_mutation_capability", {"value": 1})
            return catalog

    catalog = asyncio.run(scenario())
    projected = {tool.name: tool for tool in catalog.tools if tool.name.startswith("n8n_")}
    assert projected["n8n_read_capability"].input_schema["required"] == ["query"]
    assert projected["n8n_mutation_capability"].input_schema["required"] == ["value"]
    assert fake.calls == [
        ("read_capability", {"query": "x"}),
        ("mutation_capability", {"value": 1}),
    ]
