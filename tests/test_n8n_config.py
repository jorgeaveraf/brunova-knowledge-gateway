import json

import pytest

from app.adapters.n8n.config import N8NMCPConfig


def test_missing_n8n_configuration_is_rejected_without_secret(monkeypatch):
    monkeypatch.delenv("N8N_MCP_JSON", raising=False)
    with pytest.raises(ValueError, match="Secret Manager") as raised:
        N8NMCPConfig.from_environment()
    assert "token" not in str(raised.value).lower()


def test_invalid_n8n_configuration_does_not_echo_input():
    secret = "never-log-this-secret"
    with pytest.raises(ValueError) as raised:
        N8NMCPConfig.from_json(secret)
    assert secret not in str(raised.value)


@pytest.mark.parametrize(
    ("entry", "transport"),
    [
        ({"type": "streamableHttp", "url": "https://n8n.example/mcp", "headers": {"Authorization": "secret"}}, "streamable_http"),
        ({"type": "sse", "url": "https://n8n.example/sse"}, "sse"),
        ({"command": "n8n-mcp", "args": ["serve"], "env": {"TOKEN": "secret"}}, "stdio"),
    ],
)
def test_supported_n8n_transport_detection(entry, transport):
    config = N8NMCPConfig.from_json(json.dumps({"mcpServers": {"n8n-mcp": entry}}))
    assert config.transport == transport
    assert "secret" not in repr(config)
    assert set(config.safe_summary()) == {"configured", "transport", "has_endpoint", "authenticated"}
