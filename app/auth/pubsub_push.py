"""Authentication and parsing for authenticated Google Pub/Sub push requests."""

from __future__ import annotations

import base64
import binascii
import json
import os
from datetime import datetime

from fastapi import Request
from google.auth import exceptions as google_auth_exceptions
from google.auth.transport.requests import Request as GoogleAuthRequest
from google.oauth2 import id_token
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from app.adapters.google_workspace.errors import WorkspaceAdapterError
from app.agent_signals import AgentSignalPayload, MAX_SIGNAL_BYTES

MAX_PUSH_ENVELOPE_BYTES = 128 * 1024
VALID_GOOGLE_ISSUERS = {"accounts.google.com", "https://accounts.google.com"}


class PubSubMessage(BaseModel):
    model_config = ConfigDict(extra="ignore", populate_by_name=True)

    data: str
    message_id: str = Field(alias="messageId", min_length=1, max_length=256)
    publish_time: datetime | None = Field(default=None, alias="publishTime")
    attributes: dict[str, str] = Field(default_factory=dict)


class PubSubPushEnvelope(BaseModel):
    model_config = ConfigDict(extra="ignore")

    message: PubSubMessage
    subscription: str | None = Field(default=None, max_length=512)


class ParsedPubSubSignal(BaseModel):
    payload: AgentSignalPayload
    message_id: str
    publish_time: datetime | None
    attributes: dict[str, str]


def verify_pubsub_oidc(authorization: str | None) -> dict:
    audience = os.getenv("AGENT_SIGNAL_PUSH_AUDIENCE", "").strip()
    expected_email = os.getenv("AGENT_SIGNAL_PUSH_SERVICE_ACCOUNT", "").strip()
    if not audience or not expected_email:
        raise WorkspaceAdapterError(
            "pubsub_authentication_unavailable",
            "Pub/Sub push authentication is not configured.",
            503,
        )
    scheme, separator, token = (authorization or "").partition(" ")
    if not separator or scheme.casefold() != "bearer" or not token:
        raise WorkspaceAdapterError(
            "invalid_pubsub_authentication", "Pub/Sub authentication is required.", 401
        )
    try:
        claims = id_token.verify_oauth2_token(token, GoogleAuthRequest(), audience)
    except (ValueError, google_auth_exceptions.GoogleAuthError) as error:
        raise WorkspaceAdapterError(
            "invalid_pubsub_authentication", "Pub/Sub authentication is invalid.", 401
        ) from error
    if (
        claims.get("iss") not in VALID_GOOGLE_ISSUERS
        or claims.get("email") != expected_email
        or claims.get("email_verified") is not True
    ):
        raise WorkspaceAdapterError(
            "invalid_pubsub_identity", "Pub/Sub push identity is not authorized.", 403
        )
    return claims


async def parse_pubsub_signal(request: Request) -> ParsedPubSubSignal:
    content_length = request.headers.get("Content-Length")
    if content_length:
        try:
            if int(content_length) > MAX_PUSH_ENVELOPE_BYTES:
                raise ValueError
        except ValueError as error:
            raise WorkspaceAdapterError(
                "agent_signal_envelope_invalid", "Pub/Sub envelope is invalid.", 400
            ) from error
    raw_envelope = await request.body()
    if len(raw_envelope) > MAX_PUSH_ENVELOPE_BYTES:
        raise WorkspaceAdapterError(
            "agent_signal_envelope_invalid", "Pub/Sub envelope is too large.", 400
        )
    try:
        envelope = PubSubPushEnvelope.model_validate_json(raw_envelope)
        decoded = base64.b64decode(envelope.message.data, validate=True)
        payload = AgentSignalPayload.validate_bytes(decoded, maximum=MAX_SIGNAL_BYTES)
    except (ValidationError, ValueError, binascii.Error, json.JSONDecodeError) as error:
        raise WorkspaceAdapterError(
            "agent_signal_invalid", "Agent Signal payload is invalid.", 400
        ) from error
    return ParsedPubSubSignal(
        payload=payload,
        message_id=envelope.message.message_id,
        publish_time=envelope.message.publish_time,
        attributes=envelope.message.attributes,
    )
