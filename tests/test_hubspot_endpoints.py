from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from fastapi.testclient import TestClient

from app.adapters.hubspot.models import HubSpotConnectionStatus, HubSpotTokenResponse
from app.main import app, get_hubspot_adapter_runtime


def runtime():
    token = HubSpotTokenResponse(
        access_token="ephemeral-access-token",
        refresh_token="ephemeral-refresh-token",
        expires_in=1800,
    )
    oauth = SimpleNamespace(
        begin_authorization=AsyncMock(return_value="https://auth.example/authorize?s=safe"),
        exchange_code=AsyncMock(return_value=token),
    )
    store = SimpleNamespace(
        status=Mock(
            return_value=HubSpotConnectionStatus(
                connected=True, status="active", account={"portal_id": "safe-portal"}
            )
        ),
        update_account=Mock(),
        consume_pending=Mock(),
    )
    return SimpleNamespace(
        oauth=oauth,
        token_store=store,
        token_manager=SimpleNamespace(remember=Mock()),
        mcp_client=SimpleNamespace(call_tool=AsyncMock(return_value={})),
    )


def test_connect_and_status_require_gateway_authentication():
    app.dependency_overrides[get_hubspot_adapter_runtime] = runtime
    try:
        client = TestClient(app)
        assert client.get("/auth/hubspot/connect").status_code == 401
        assert client.get("/auth/hubspot/status").status_code == 401
    finally:
        app.dependency_overrides.clear()


def test_callback_is_public_and_never_returns_tokens():
    fake = runtime()
    app.dependency_overrides[get_hubspot_adapter_runtime] = lambda: fake
    try:
        response = TestClient(app).get(
            "/auth/hubspot/callback",
            params={"state": "s" * 40, "code": "authorization-code"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert "HubSpot conectado" in response.text
    assert "ephemeral-access-token" not in response.text
    assert "ephemeral-refresh-token" not in response.text
    fake.oauth.exchange_code.assert_awaited_once()


def test_status_returns_only_safe_connection_metadata(ephemeral_gateway_token):
    app.dependency_overrides[get_hubspot_adapter_runtime] = runtime
    try:
        response = TestClient(app).get(
            "/auth/hubspot/status",
            headers={"Authorization": f"Bearer {ephemeral_gateway_token}"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "connected": True,
        "provider": "hubspot",
        "account": {
            "portal_id": "safe-portal",
            "account_name": None,
            "user_id": None,
            "user_email": None,
        },
        "status": "active",
    }
