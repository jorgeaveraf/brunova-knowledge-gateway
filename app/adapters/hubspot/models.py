"""Safe models for the HubSpot OAuth and MCP boundary."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Literal

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class PendingAuthorization(BaseModel):
    state_digest: str
    code_verifier: str = Field(min_length=43, max_length=128, repr=False)
    created_at: datetime
    expires_at: datetime


class HubSpotAccountMetadata(BaseModel):
    portal_id: str | None = None
    account_name: str | None = None
    user_id: str | None = None
    user_email: str | None = None


class HubSpotConnectionDocument(BaseModel):
    version: Literal[1] = 1
    encrypted_refresh_token: str = Field(repr=False)
    token_scope: str | None = None
    connected_at: datetime
    updated_at: datetime
    account: HubSpotAccountMetadata = Field(default_factory=HubSpotAccountMetadata)
    status: Literal["active", "refreshing", "reauthorization_required"] = "active"
    refresh_lock_id: str | None = None
    refresh_lock_expires_at: datetime | None = None


class HubSpotConnectionStatus(BaseModel):
    connected: bool
    provider: Literal["hubspot"] = "hubspot"
    account: HubSpotAccountMetadata | None = None
    status: Literal["active", "disconnected", "reauthorization_required"]


class HubSpotTokenResponse(BaseModel):
    access_token: str = Field(repr=False)
    token_type: str = "Bearer"
    expires_in: int | None = None
    refresh_token: str | None = Field(default=None, repr=False)
    scope: str | None = None


class AccessToken(BaseModel):
    value: str = Field(repr=False)
    expires_at: datetime | None = None


class RefreshLease(BaseModel):
    lock_id: str
    generation: int
    refresh_token: str = Field(repr=False)
    scope: str | None = None


class HubSpotToolDescriptor(BaseModel):
    name: str
    description: str | None = None
    input_schema: dict[str, Any] = Field(default_factory=dict)
    classification: Literal["read", "mutation", "unknown"]
    allowed: bool
    approval_required: bool


class HubSpotToolResult(BaseModel):
    provider: Literal["hubspot"] = "hubspot"
    tool: str
    result: Any
    request_id: str
