"""HubSpot OAuth 2.1 authorization-code flow with PKCE."""

from __future__ import annotations

import base64
import hashlib
import secrets
from dataclasses import dataclass
from datetime import timedelta
from typing import Any
from urllib.parse import quote, urlencode, urljoin, urlparse

import httpx2

from app.adapters.google_workspace.errors import WorkspaceAdapterError
from app.adapters.hubspot.models import (
    HubSpotTokenResponse,
    PendingAuthorization,
    utc_now,
)
from app.adapters.hubspot.token_store import HubSpotTokenStore


@dataclass(frozen=True, repr=False)
class HubSpotClientCredentials:
    client_id: str
    client_secret: str


@dataclass(frozen=True)
class OAuthEndpoints:
    authorization_endpoint: str
    token_endpoint: str
    token_endpoint_auth_method: str = "client_secret_post"


class HubSpotOAuthClient:
    def __init__(
        self,
        *,
        server_url: str,
        redirect_uri: str,
        credentials: HubSpotClientCredentials,
        token_store: HubSpotTokenStore,
        state_ttl_seconds: int = 600,
        http_client_factory=httpx2.AsyncClient,
    ) -> None:
        self.server_url = server_url.rstrip("/")
        self.redirect_uri = redirect_uri
        self.credentials = credentials
        self.token_store = token_store
        self.state_ttl_seconds = state_ttl_seconds
        self._http_client_factory = http_client_factory
        self._endpoints: OAuthEndpoints | None = None

    async def begin_authorization(self) -> str:
        state = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(72)[:96]
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode()).digest()
        ).decode().rstrip("=")
        now = utc_now()
        self.token_store.create_pending(
            state,
            PendingAuthorization(
                state_digest=hashlib.sha256(state.encode()).hexdigest(),
                code_verifier=verifier,
                created_at=now,
                expires_at=now + timedelta(seconds=self.state_ttl_seconds),
            ),
        )
        endpoints = await self.discover_endpoints()
        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.credentials.client_id,
                "redirect_uri": self.redirect_uri,
                "state": state,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
                "resource": self.server_url,
            }
        )
        return f"{endpoints.authorization_endpoint}?{query}"

    async def exchange_code(self, *, code: str, state: str) -> HubSpotTokenResponse:
        pending = self.token_store.consume_pending(state)
        endpoints = await self.discover_endpoints()
        token = await self._token_request(
            endpoints.token_endpoint,
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.redirect_uri,
                "client_id": self.credentials.client_id,
                "client_secret": self.credentials.client_secret,
                "code_verifier": pending.code_verifier,
                "resource": self.server_url,
            },
        )
        if not token.refresh_token:
            raise WorkspaceAdapterError(
                "hubspot_oauth_token_invalid",
                "HubSpot did not return a durable connection token.",
                502,
            )
        self.token_store.save_connection(
            token.refresh_token,
            scope=token.scope,
        )
        return token

    async def refresh(self, refresh_token: str) -> HubSpotTokenResponse:
        endpoints = await self.discover_endpoints()
        return await self._token_request(
            endpoints.token_endpoint,
            {
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
                "client_id": self.credentials.client_id,
                "client_secret": self.credentials.client_secret,
                "resource": self.server_url,
            },
            refresh=True,
        )

    async def discover_endpoints(self) -> OAuthEndpoints:
        if self._endpoints is not None:
            return self._endpoints
        base = _origin(self.server_url)
        authorization_servers: list[str] = []
        async with self._http_client_factory(follow_redirects=True, timeout=15.0) as client:
            for url in (
                f"{base}/.well-known/oauth-protected-resource",
                f"{base}/.well-known/oauth-protected-resource{urlparse(self.server_url).path}",
            ):
                try:
                    response = await client.get(url)
                    if response.status_code == 200:
                        payload = response.json()
                        authorization_servers.extend(payload.get("authorization_servers", []))
                        if authorization_servers:
                            break
                except Exception:
                    continue
            for issuer in authorization_servers or [base]:
                issuer = issuer.rstrip("/")
                for url in (
                    f"{issuer}/.well-known/oauth-authorization-server",
                    f"{issuer}/.well-known/openid-configuration",
                ):
                    try:
                        response = await client.get(url)
                        if response.status_code != 200:
                            continue
                        payload = response.json()
                        authorization = payload.get("authorization_endpoint")
                        token = payload.get("token_endpoint")
                        if authorization and token:
                            supported = payload.get(
                                "token_endpoint_auth_methods_supported", []
                            )
                            auth_method = (
                                "client_secret_basic"
                                if "client_secret_basic" in supported
                                else "client_secret_post"
                            )
                            self._endpoints = OAuthEndpoints(
                                authorization, token, auth_method
                            )
                            return self._endpoints
                    except Exception:
                        continue
        self._endpoints = OAuthEndpoints(
            urljoin(base, "/authorize"),
            urljoin(base, "/token"),
        )
        return self._endpoints

    async def _token_request(
        self,
        endpoint: str,
        data: dict[str, str],
        *,
        refresh: bool = False,
    ) -> HubSpotTokenResponse:
        try:
            headers = {"Content-Type": "application/x-www-form-urlencoded"}
            endpoints = await self.discover_endpoints()
            if endpoints.token_endpoint_auth_method == "client_secret_basic":
                raw_credentials = (
                    f"{quote(self.credentials.client_id, safe='')}:"
                    f"{quote(self.credentials.client_secret, safe='')}"
                )
                headers["Authorization"] = (
                    "Basic " + base64.b64encode(raw_credentials.encode()).decode()
                )
                data = {
                    key: value
                    for key, value in data.items()
                    if key != "client_secret"
                }
            async with self._http_client_factory(timeout=20.0) as client:
                response = await client.post(
                    endpoint,
                    data=data,
                    headers=headers,
                )
            if response.status_code not in (200, 201):
                code = "hubspot_refresh_invalid" if refresh and response.status_code in (400, 401) else "hubspot_oauth_exchange_failed"
                status = 401 if code == "hubspot_refresh_invalid" else 502
                raise WorkspaceAdapterError(code, "HubSpot authorization could not be completed.", status)
            return HubSpotTokenResponse.model_validate(response.json())
        except WorkspaceAdapterError:
            raise
        except Exception as error:
            raise WorkspaceAdapterError(
                "hubspot_oauth_unavailable", "HubSpot authorization is unavailable.", 502
            ) from error


def _origin(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.netloc}"
