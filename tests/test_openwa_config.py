import pytest

from app.adapters.openwa.config import OpenWAMCPConfig


def test_missing_openwa_configuration_is_rejected_without_secret(monkeypatch):
    monkeypatch.delenv("OPENWA_MCP_URL", raising=False)
    monkeypatch.delenv("OPENWA_APIKEY", raising=False)

    with pytest.raises(ValueError) as raised:
        OpenWAMCPConfig.from_environment()

    assert "OPENWA_MCP_URL" in str(raised.value)
    assert "OPENWA_APIKEY" in str(raised.value)


@pytest.mark.parametrize(
    "url",
    ["wa.example/mcp", "ftp://wa.example/mcp", "https://wa.example/api", "https://wa.example/mcp?key=x"],
)
def test_invalid_openwa_endpoint_does_not_echo_secret(monkeypatch, url):
    monkeypatch.setenv("OPENWA_MCP_URL", url)
    monkeypatch.setenv("OPENWA_APIKEY", "top-secret-openwa-key")

    with pytest.raises(ValueError) as raised:
        OpenWAMCPConfig.from_environment()

    assert "top-secret-openwa-key" not in str(raised.value)


def test_openwa_config_hides_api_key(monkeypatch):
    monkeypatch.setenv("OPENWA_MCP_URL", "https://wa.example/mcp/")
    monkeypatch.setenv("OPENWA_APIKEY", "top-secret-openwa-key")

    config = OpenWAMCPConfig.from_environment()

    assert "top-secret-openwa-key" not in repr(config)
    assert config.safe_summary() == {
        "configured": True,
        "transport": "streamable_http",
        "has_endpoint": True,
        "authenticated": True,
    }
