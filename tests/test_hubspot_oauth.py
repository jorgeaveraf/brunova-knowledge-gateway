import hashlib
import asyncio
from urllib.parse import parse_qs, urlparse

from app.adapters.hubspot.oauth import (
    HubSpotClientCredentials,
    HubSpotOAuthClient,
    OAuthEndpoints,
)


class RecordingStore:
    def __init__(self):
        self.state = None
        self.pending = None

    def create_pending(self, state, pending):
        self.state = state
        self.pending = pending


def test_connect_generates_state_and_s256_pkce():
    store = RecordingStore()
    oauth = HubSpotOAuthClient(
        server_url="https://mcp.hubspot.com",
        redirect_uri="https://gateway.example/auth/hubspot/callback",
        credentials=HubSpotClientCredentials("test-client", "test-secret"),
        token_store=store,
    )
    oauth._endpoints = OAuthEndpoints(
        "https://auth.example/authorize", "https://auth.example/token"
    )

    authorization_url = asyncio.run(oauth.begin_authorization())
    query = parse_qs(urlparse(authorization_url).query)
    expected_challenge = hashlib.sha256(store.pending.code_verifier.encode()).digest()
    import base64

    assert query["state"] == [store.state]
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"] == [
        base64.urlsafe_b64encode(expected_challenge).decode().rstrip("=")
    ]
    assert query["redirect_uri"] == ["https://gateway.example/auth/hubspot/callback"]
    assert "test-secret" not in authorization_url


def test_credentials_repr_does_not_expose_secret():
    credentials = HubSpotClientCredentials("client-id", "do-not-expose")
    assert "do-not-expose" not in repr(credentials)
