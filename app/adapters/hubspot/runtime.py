"""HubSpot adapter construction isolated from the Workspace runtime."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache

from app.adapters.hubspot.mcp_client import HubSpotMCPClient, HubSpotTokenManager
from app.adapters.hubspot.oauth import HubSpotClientCredentials, HubSpotOAuthClient
from app.adapters.hubspot.token_store import (
    CloudStorageOAuthStateBackend,
    HubSpotTokenStore,
)
from app.config.settings import get_settings
from app.policies.hubspot import HubSpotToolPolicy


@dataclass(frozen=True)
class HubSpotRuntime:
    oauth: HubSpotOAuthClient
    token_store: HubSpotTokenStore
    token_manager: HubSpotTokenManager
    mcp_client: HubSpotMCPClient
    policy: HubSpotToolPolicy


@lru_cache
def get_hubspot_runtime() -> HubSpotRuntime:
    settings = get_settings()
    client_secret = os.getenv("HUBSPOT_MCP_CLIENT_SECRET", "").strip()
    if not settings.hubspot_mcp_client_id:
        raise ValueError("HUBSPOT_MCP_CLIENT_ID must be configured")
    if not client_secret:
        raise ValueError("HUBSPOT_MCP_CLIENT_SECRET must be injected from Secret Manager")
    if not settings.hubspot_mcp_redirect_uri:
        raise ValueError("HUBSPOT_MCP_REDIRECT_URI must be configured")
    credentials = HubSpotClientCredentials(
        client_id=settings.hubspot_mcp_client_id,
        client_secret=client_secret,
    )
    token_store = HubSpotTokenStore(
        CloudStorageOAuthStateBackend(bucket_name=settings.hubspot_oauth_state_bucket),
        prefix=settings.hubspot_oauth_state_prefix,
        encryption_secret=client_secret,
    )
    oauth = HubSpotOAuthClient(
        server_url=settings.hubspot_mcp_server_url,
        redirect_uri=settings.hubspot_mcp_redirect_uri,
        credentials=credentials,
        token_store=token_store,
        state_ttl_seconds=settings.hubspot_oauth_state_ttl_seconds,
    )
    token_manager = HubSpotTokenManager(oauth, token_store)
    policy = HubSpotToolPolicy()
    return HubSpotRuntime(
        oauth=oauth,
        token_store=token_store,
        token_manager=token_manager,
        mcp_client=HubSpotMCPClient(
            server_url=settings.hubspot_mcp_server_url,
            token_manager=token_manager,
            policy=policy,
        ),
        policy=policy,
    )
