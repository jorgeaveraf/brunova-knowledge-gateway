"""Atomic, encrypted HubSpot OAuth state persisted in Cloud Storage."""

from __future__ import annotations

import base64
import hashlib
import os
from datetime import timedelta
from typing import Protocol
from uuid import uuid4

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from google.api_core.exceptions import GoogleAPIError, NotFound, PreconditionFailed
from google.cloud import storage

from app.adapters.google_workspace.errors import WorkspaceAdapterError
from app.adapters.hubspot.models import (
    HubSpotAccountMetadata,
    HubSpotConnectionDocument,
    HubSpotConnectionStatus,
    PendingAuthorization,
    RefreshLease,
    utc_now,
)


class ObjectConflict(RuntimeError):
    pass


class OAuthStateBackend(Protocol):
    def read(self, object_name: str) -> tuple[bytes | None, int]: ...
    def write(self, object_name: str, content: bytes, *, generation: int) -> int: ...
    def delete(self, object_name: str, *, generation: int) -> None: ...


class CloudStorageOAuthStateBackend:
    def __init__(self, *, bucket_name: str, client: storage.Client | None = None) -> None:
        if not bucket_name:
            raise ValueError("HUBSPOT_OAUTH_STATE_BUCKET must be configured")
        self._bucket = (client or storage.Client()).bucket(bucket_name)

    def read(self, object_name: str) -> tuple[bytes | None, int]:
        try:
            blob = self._bucket.blob(object_name)
            content = blob.download_as_bytes()
            return content, int(blob.generation or 0)
        except NotFound:
            return None, 0
        except GoogleAPIError as error:
            raise _unavailable() from error

    def write(self, object_name: str, content: bytes, *, generation: int) -> int:
        try:
            blob = self._bucket.blob(object_name)
            blob.upload_from_string(
                content,
                content_type="application/json",
                if_generation_match=generation,
            )
            return int(blob.generation or generation + 1)
        except PreconditionFailed as error:
            raise ObjectConflict from error
        except GoogleAPIError as error:
            raise _unavailable() from error

    def delete(self, object_name: str, *, generation: int) -> None:
        try:
            self._bucket.blob(object_name).delete(if_generation_match=generation)
        except (NotFound, PreconditionFailed) as error:
            raise ObjectConflict from error
        except GoogleAPIError as error:
            raise _unavailable() from error


class HubSpotTokenStore:
    """Stores only encrypted refresh tokens; access tokens stay in process memory."""

    CONNECTION_FILE = "connection.json"

    def __init__(self, backend: OAuthStateBackend, *, prefix: str, encryption_secret: str) -> None:
        clean_prefix = prefix.strip("/")
        if not clean_prefix or ".." in clean_prefix:
            raise ValueError("HUBSPOT_OAUTH_STATE_PREFIX is invalid")
        if not encryption_secret:
            raise ValueError("HubSpot client secret is unavailable")
        self._backend = backend
        self._prefix = clean_prefix
        self._key = hashlib.sha256(
            b"brunova-hubspot-refresh-token-v1\x00" + encryption_secret.encode()
        ).digest()

    def create_pending(self, state: str, pending: PendingAuthorization) -> None:
        name = self._pending_name(state)
        try:
            self._backend.write(name, pending.model_dump_json().encode(), generation=0)
        except ObjectConflict as error:
            raise WorkspaceAdapterError(
                "hubspot_oauth_state_conflict", "OAuth authorization could not be started.", 409
            ) from error

    def consume_pending(self, state: str) -> PendingAuthorization:
        name = self._pending_name(state)
        content, generation = self._backend.read(name)
        if content is None:
            raise WorkspaceAdapterError(
                "hubspot_oauth_state_invalid", "OAuth state is invalid or already used.", 400
            )
        try:
            pending = PendingAuthorization.model_validate_json(content)
        except Exception as error:
            raise WorkspaceAdapterError(
                "hubspot_oauth_state_invalid", "OAuth state is invalid.", 400
            ) from error
        if pending.state_digest != _state_digest(state):
            raise WorkspaceAdapterError(
                "hubspot_oauth_state_invalid", "OAuth state is invalid.", 400
            )
        try:
            self._backend.delete(name, generation=generation)
        except ObjectConflict as error:
            raise WorkspaceAdapterError(
                "hubspot_oauth_state_invalid", "OAuth state is invalid or already used.", 400
            ) from error
        if pending.expires_at <= utc_now():
            raise WorkspaceAdapterError(
                "hubspot_oauth_state_expired", "OAuth state has expired.", 400
            )
        return pending

    def save_connection(
        self,
        refresh_token: str,
        *,
        scope: str | None,
        account: HubSpotAccountMetadata | None = None,
    ) -> None:
        _current, generation = self._read_connection()
        now = utc_now()
        document = HubSpotConnectionDocument(
            encrypted_refresh_token=self._encrypt(refresh_token),
            token_scope=scope,
            connected_at=now,
            updated_at=now,
            account=account or HubSpotAccountMetadata(),
        )
        try:
            self._write_connection(document, generation=generation)
        except ObjectConflict as error:
            raise WorkspaceAdapterError(
                "hubspot_connection_conflict", "HubSpot connection changed concurrently.", 409
            ) from error

    def status(self) -> HubSpotConnectionStatus:
        document, _generation = self._read_connection()
        if document is None:
            return HubSpotConnectionStatus(connected=False, status="disconnected")
        safe_status = (
            "reauthorization_required"
            if document.status == "reauthorization_required"
            else "active"
        )
        return HubSpotConnectionStatus(
            connected=safe_status == "active",
            account=document.account,
            status=safe_status,
        )

    def acquire_refresh_lease(self, *, ttl_seconds: int = 30) -> RefreshLease:
        document, generation = self._read_connection()
        if document is None or document.status == "reauthorization_required":
            raise WorkspaceAdapterError(
                "hubspot_not_connected", "HubSpot authorization is required.", 409
            )
        now = utc_now()
        if (
            document.status == "refreshing"
            and document.refresh_lock_expires_at
            and document.refresh_lock_expires_at > now
        ):
            raise WorkspaceAdapterError(
                "hubspot_refresh_in_progress", "HubSpot token refresh is already in progress.", 409
            )
        lock_id = uuid4().hex
        document.status = "refreshing"
        document.refresh_lock_id = lock_id
        document.refresh_lock_expires_at = now + timedelta(seconds=ttl_seconds)
        document.updated_at = now
        try:
            new_generation = self._write_connection(document, generation=generation)
        except ObjectConflict as error:
            raise WorkspaceAdapterError(
                "hubspot_refresh_in_progress", "HubSpot token refresh is already in progress.", 409
            ) from error
        return RefreshLease(
            lock_id=lock_id,
            generation=new_generation,
            refresh_token=self._decrypt(document.encrypted_refresh_token),
            scope=document.token_scope,
        )

    def complete_refresh(self, lease: RefreshLease, refresh_token: str, *, scope: str | None) -> None:
        document, generation = self._read_connection()
        if document is None or generation != lease.generation or document.refresh_lock_id != lease.lock_id:
            raise WorkspaceAdapterError(
                "hubspot_refresh_conflict", "HubSpot token refresh state changed concurrently.", 409
            )
        document.encrypted_refresh_token = self._encrypt(refresh_token)
        document.token_scope = scope or lease.scope
        document.status = "active"
        document.refresh_lock_id = None
        document.refresh_lock_expires_at = None
        document.updated_at = utc_now()
        try:
            self._write_connection(document, generation=generation)
        except ObjectConflict as error:
            raise WorkspaceAdapterError(
                "hubspot_refresh_conflict", "HubSpot token refresh state changed concurrently.", 409
            ) from error

    def fail_refresh(self, lease: RefreshLease, *, invalid: bool) -> None:
        document, generation = self._read_connection()
        if document is None or generation != lease.generation or document.refresh_lock_id != lease.lock_id:
            return
        document.status = "reauthorization_required" if invalid else "active"
        document.refresh_lock_id = None
        document.refresh_lock_expires_at = None
        document.updated_at = utc_now()
        try:
            self._write_connection(document, generation=generation)
        except ObjectConflict:
            return

    def update_account(self, account: HubSpotAccountMetadata) -> None:
        document, generation = self._read_connection()
        if document is None:
            return
        document.account = account
        document.updated_at = utc_now()
        try:
            self._write_connection(document, generation=generation)
        except ObjectConflict:
            return

    def _read_connection(self) -> tuple[HubSpotConnectionDocument | None, int]:
        content, generation = self._backend.read(f"{self._prefix}/{self.CONNECTION_FILE}")
        if content is None:
            return None, generation
        try:
            return HubSpotConnectionDocument.model_validate_json(content), generation
        except Exception as error:
            raise WorkspaceAdapterError(
                "hubspot_connection_invalid", "HubSpot connection state is invalid.", 503
            ) from error

    def _write_connection(self, document: HubSpotConnectionDocument, *, generation: int) -> int:
        return self._backend.write(
            f"{self._prefix}/{self.CONNECTION_FILE}",
            document.model_dump_json().encode(),
            generation=generation,
        )

    def _pending_name(self, state: str) -> str:
        return f"{self._prefix}/pending/{_state_digest(state)}.json"

    def _encrypt(self, value: str) -> str:
        nonce = os.urandom(12)
        encrypted = AESGCM(self._key).encrypt(nonce, value.encode(), self._prefix.encode())
        return base64.urlsafe_b64encode(nonce + encrypted).decode()

    def _decrypt(self, value: str) -> str:
        try:
            payload = base64.urlsafe_b64decode(value)
            return AESGCM(self._key).decrypt(
                payload[:12], payload[12:], self._prefix.encode()
            ).decode()
        except Exception as error:
            raise WorkspaceAdapterError(
                "hubspot_connection_invalid", "HubSpot connection state is invalid.", 503
            ) from error


def _state_digest(state: str) -> str:
    return hashlib.sha256(state.encode()).hexdigest()


def _unavailable() -> WorkspaceAdapterError:
    return WorkspaceAdapterError(
        "hubspot_state_unavailable", "HubSpot authorization state is unavailable.", 503
    )
