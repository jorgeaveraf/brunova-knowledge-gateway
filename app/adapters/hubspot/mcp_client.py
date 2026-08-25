"""Official-SDK downstream client for HubSpot Remote MCP."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import timedelta
from typing import Any

import anyio
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client

from app.adapters.google_workspace.errors import WorkspaceAdapterError
from app.adapters.hubspot.models import (
    AccessToken,
    HubSpotAccountMetadata,
    HubSpotToolDescriptor,
    HubSpotTokenResponse,
    utc_now,
)
from app.adapters.hubspot.oauth import HubSpotOAuthClient
from app.adapters.hubspot.token_store import HubSpotTokenStore
from app.policies.hubspot import HubSpotToolPolicy


class HubSpotTokenManager:
    def __init__(self, oauth: HubSpotOAuthClient, store: HubSpotTokenStore) -> None:
        self._oauth = oauth
        self._store = store
        self._access_token: AccessToken | None = None
        self._lock = anyio.Lock()

    def remember(self, token: HubSpotTokenResponse) -> None:
        expires_at = (
            utc_now() + timedelta(seconds=max(token.expires_in - 60, 1))
            if token.expires_in
            else None
        )
        self._access_token = AccessToken(value=token.access_token, expires_at=expires_at)

    async def get_access_token(self) -> str:
        if self._is_valid():
            return self._access_token.value  # type: ignore[union-attr]
        async with self._lock:
            if self._is_valid():
                return self._access_token.value  # type: ignore[union-attr]
            lease = None
            for attempt in range(5):
                try:
                    lease = self._store.acquire_refresh_lease()
                    break
                except WorkspaceAdapterError as error:
                    if error.code != "hubspot_refresh_in_progress" or attempt == 4:
                        raise
                    await anyio.sleep(0.25 * (attempt + 1))
            if lease is None:  # pragma: no cover
                raise WorkspaceAdapterError(
                    "hubspot_refresh_in_progress", "HubSpot token refresh is in progress.", 409
                )
            try:
                token = await self._oauth.refresh(lease.refresh_token)
                rotated_refresh = token.refresh_token or lease.refresh_token
                self._store.complete_refresh(
                    lease,
                    rotated_refresh,
                    scope=token.scope,
                )
                self.remember(token)
                return token.access_token
            except WorkspaceAdapterError as error:
                self._store.fail_refresh(
                    lease,
                    invalid=error.code == "hubspot_refresh_invalid",
                )
                raise
            except Exception:
                self._store.fail_refresh(lease, invalid=False)
                raise

    def _is_valid(self) -> bool:
        return bool(
            self._access_token
            and (
                self._access_token.expires_at is None
                or self._access_token.expires_at > utc_now()
            )
        )


class HubSpotMCPClient:
    def __init__(
        self,
        *,
        server_url: str,
        token_manager: HubSpotTokenManager,
        policy: HubSpotToolPolicy,
        client_factory: Callable[[str], Any] | None = None,
    ) -> None:
        self.server_url = server_url
        self.token_manager = token_manager
        self.policy = policy
        self._client_factory = client_factory or self._default_client

    async def list_tools(self) -> list[HubSpotToolDescriptor]:
        async with self._client_factory(await self.token_manager.get_access_token()) as client:
            tools = []
            cursor = None
            while True:
                page = await client.list_tools(cursor=cursor, cache_mode="bypass")
                tools.extend(page.tools)
                cursor = page.next_cursor
                if not cursor:
                    break
        return [
            HubSpotToolDescriptor(
                name=tool.name,
                description=tool.description,
                input_schema=getattr(tool, "input_schema", {}) or {},
                classification=(decision := self.policy.classify(tool.name)).classification,
                allowed=decision.allowed,
                approval_required=decision.approval_required,
            )
            for tool in tools
        ]

    async def call_tool(
        self,
        tool_name: str,
        arguments: dict[str, Any],
        *,
        approval_reference: str | None = None,
        explicit_intent: bool = False,
    ) -> Any:
        self.policy.authorize(
            tool_name,
            arguments,
            approval_reference=approval_reference,
            explicit_intent=explicit_intent,
        )
        access_token = await self.token_manager.get_access_token()
        async with self._client_factory(access_token) as client:
            page = await client.list_tools(cache_mode="bypass")
            available = {tool.name for tool in page.tools}
            cursor = page.next_cursor
            while cursor and tool_name not in available:
                page = await client.list_tools(cursor=cursor, cache_mode="bypass")
                available.update(tool.name for tool in page.tools)
                cursor = page.next_cursor
            if tool_name not in available:
                raise WorkspaceAdapterError(
                    "hubspot_tool_unavailable",
                    "The HubSpot tool is not available for this connection.",
                    403,
                )
            result = await client.call_tool(tool_name, arguments)
            return result.model_dump(mode="json", by_alias=True)

    @asynccontextmanager
    async def _default_client(self, access_token: str) -> AsyncIterator[Client]:
        async with httpx2.AsyncClient(
            headers={"Authorization": f"Bearer {access_token}"},
            follow_redirects=True,
        ) as http_client:
            transport = streamable_http_client(
                self.server_url,
                http_client=http_client,
            )
            async with Client(transport, mode="legacy") as client:
                yield client


def account_metadata_from_result(result: Any) -> HubSpotAccountMetadata:
    """Extract only explicitly safe identity fields from an MCP result."""

    values: dict[str, Any] = {}

    def visit(value: Any, *, depth: int = 0) -> None:
        if depth > 10:
            return
        if isinstance(value, dict):
            for key, child in value.items():
                normalized = key.replace("_", "").casefold()
                if normalized in {"portalid", "accountid", "hubid"} and "portal_id" not in values:
                    values["portal_id"] = str(child)
                elif normalized in {"accountname", "portalname"} and "account_name" not in values:
                    values["account_name"] = str(child)
                elif normalized == "userid" and "user_id" not in values:
                    values["user_id"] = str(child)
                elif normalized in {"useremail", "email"} and "user_email" not in values:
                    values["user_email"] = str(child)
                visit(child, depth=depth + 1)
        elif isinstance(value, list):
            for child in value:
                visit(child, depth=depth + 1)
        elif isinstance(value, str) and len(value) <= 1_000_000:
            stripped = value.strip()
            if stripped.startswith(("{", "[")):
                try:
                    visit(json.loads(stripped), depth=depth + 1)
                except json.JSONDecodeError:
                    return

    visit(result)
    return HubSpotAccountMetadata.model_validate(values)
