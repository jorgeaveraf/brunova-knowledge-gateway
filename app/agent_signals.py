"""Durable, idempotent Agent Signal Inbox backed by Cloud Storage."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from enum import StrEnum
from typing import Any, Callable, Protocol

from google.api_core.exceptions import GoogleAPIError, NotFound, PreconditionFailed
from google.cloud import storage
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)

from app.adapters.google_workspace.errors import WorkspaceAdapterError

MAX_SIGNAL_BYTES = 64 * 1024
MAX_LIST_LIMIT = 100
DEFAULT_CLAIM_LEASE_SECONDS = 30 * 60
DEFAULT_COMPLETED_RETENTION_DAYS = 30
MAX_WRITE_ATTEMPTS = 5


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class SignalPriority(StrEnum):
    ATTENTION = "attention"
    URGENT = "urgent"


class SignalStatus(StrEnum):
    PENDING = "pending"
    CLAIMED = "claimed"
    COMPLETED = "completed"
    DISMISSED = "dismissed"


class WhatsAppContact(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sender_phone: str = Field(min_length=1, max_length=64)
    sender_name: str = Field(min_length=1, max_length=256)
    hubspot_contact_id: str | None = Field(default=None, max_length=128)

    @field_validator("hubspot_contact_id", mode="before")
    @classmethod
    def normalize_optional_hubspot_contact_id(cls, value: Any) -> Any:
        if value is None or not isinstance(value, str):
            return value
        normalized = value.strip()
        return normalized or None


class WhatsAppConversation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=1, max_length=256)
    chat_id: str = Field(min_length=1, max_length=256)
    message_id: str = Field(min_length=1, max_length=256)


class AgentSignalPayload(BaseModel):
    """Common signal contract plus the v1 allowlisted WhatsApp extension."""

    model_config = ConfigDict(extra="forbid")

    signal_id: str = Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
    signal_type: str = Field(pattern=r"^[a-z][a-z0-9_]{2,127}$")
    priority: SignalPriority
    occurred_at: datetime
    source: str = Field(pattern=r"^[a-z][a-z0-9_-]{1,63}$")
    reason: dict[str, Any] = Field(min_length=1)
    references: dict[str, str] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)
    contact: WhatsAppContact | None = None
    conversation: WhatsAppConversation | None = None
    preview: str | None = Field(default=None, max_length=500)

    @field_validator("occurred_at")
    @classmethod
    def occurred_at_must_be_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("occurred_at must include a timezone")
        return value

    @model_validator(mode="after")
    def validate_type_specific_contract(self) -> "AgentSignalPayload":
        if self.signal_type == "whatsapp_attention_required":
            if self.source != "openwa":
                raise ValueError("WhatsApp signals must use source=openwa")
            if self.contact is None or self.conversation is None:
                raise ValueError("WhatsApp signals require contact and conversation")
            chat_type = self.metadata.get("chat_type")
            if chat_type not in {"direct", "group", "unknown"}:
                raise ValueError("WhatsApp signals require a valid metadata.chat_type")
            if not isinstance(self.metadata.get("is_test"), bool):
                raise ValueError("WhatsApp signals require boolean metadata.is_test")
            assert self.contact is not None and self.conversation is not None
            expected_references = {
                "session_id": self.conversation.session_id,
                "chat_id": self.conversation.chat_id,
                "message_id": self.conversation.message_id,
            }
            if self.contact.hubspot_contact_id:
                expected_references["hubspot_contact_id"] = (
                    self.contact.hubspot_contact_id
                )
            for key, value in expected_references.items():
                existing = self.references.get(key)
                if existing is not None and existing != value:
                    raise ValueError(f"WhatsApp references.{key} conflicts with payload")
                self.references[key] = value
        return self

    @classmethod
    def validate_bytes(cls, raw: bytes, *, maximum: int = MAX_SIGNAL_BYTES) -> "AgentSignalPayload":
        if len(raw) > maximum:
            raise ValueError("Agent Signal payload is too large")
        return cls.model_validate_json(raw)


class AgentSignalRecord(AgentSignalPayload):
    model_config = ConfigDict(extra="forbid")

    status: SignalStatus = SignalStatus.PENDING
    received_at: datetime
    updated_at: datetime
    claimed_by: str | None = None
    claimed_at: datetime | None = None
    claim_expires_at: datetime | None = None
    completed_at: datetime | None = None
    dismissed_at: datetime | None = None
    completion_summary: str | None = Field(default=None, max_length=1000)
    dismissal_reason: str | None = Field(default=None, max_length=500)
    outcome_metadata: dict[str, Any] = Field(default_factory=dict)
    pubsub_message_id: str = Field(min_length=1, max_length=256)
    pubsub_publish_time: datetime | None = None
    pubsub_attributes: dict[str, str] = Field(default_factory=dict)

    @classmethod
    def from_delivery(
        cls,
        payload: AgentSignalPayload,
        *,
        message_id: str,
        publish_time: datetime | None,
        attributes: dict[str, str],
        now: datetime,
    ) -> "AgentSignalRecord":
        return cls(
            **payload.model_dump(),
            received_at=now,
            updated_at=now,
            pubsub_message_id=message_id,
            pubsub_publish_time=publish_time,
            pubsub_attributes=attributes,
        )


class StoredSignal(BaseModel):
    record: AgentSignalRecord
    generation: int


class AgentSignalDeliveryResult(BaseModel):
    signal_id: str
    signal_type: str
    created: bool
    status: SignalStatus


class AgentSignalListResult(BaseModel):
    signals: list[AgentSignalRecord]


class AgentSignalStatusResult(BaseModel):
    configured: bool
    pubsub_push_configured: bool
    inbox_available: bool
    pending_count: int
    urgent_count: int
    claimed_count: int
    request_id: str | None = None


class AgentSignalObjectConflict(RuntimeError):
    pass


class AgentSignalObjectBackend(Protocol):
    def create(self, record: AgentSignalRecord) -> bool: ...
    def read(self, signal_id: str) -> StoredSignal: ...
    def list(self) -> list[StoredSignal]: ...
    def replace(self, record: AgentSignalRecord, *, generation: int) -> None: ...
    def delete(self, signal_id: str, *, generation: int) -> None: ...


class CloudStorageAgentSignalBackend:
    def __init__(
        self,
        *,
        bucket_name: str,
        prefix: str = "agent-signals/items/",
        client: storage.Client | None = None,
    ) -> None:
        if not bucket_name:
            raise ValueError("AGENT_SIGNAL_BUCKET must be configured")
        if not prefix or prefix.startswith("/") or ".." in prefix:
            raise ValueError("AGENT_SIGNAL_PREFIX is invalid")
        self._bucket = (client or storage.Client()).bucket(bucket_name)
        self._prefix = prefix.rstrip("/") + "/"

    def _blob(self, signal_id: str):
        return self._bucket.blob(f"{self._prefix}{signal_id}.json")

    def create(self, record: AgentSignalRecord) -> bool:
        try:
            self._blob(record.signal_id).upload_from_string(
                _serialize(record),
                content_type="application/json; charset=utf-8",
                if_generation_match=0,
            )
            return True
        except PreconditionFailed:
            return False
        except GoogleAPIError as error:
            raise _store_unavailable() from error

    def read(self, signal_id: str) -> StoredSignal:
        blob = self._blob(signal_id)
        try:
            raw = blob.download_as_bytes()
            return _stored(raw, int(blob.generation or 0))
        except NotFound as error:
            raise WorkspaceAdapterError(
                "agent_signal_not_found", "The requested Agent Signal was not found.", 404
            ) from error
        except GoogleAPIError as error:
            raise _store_unavailable() from error

    def list(self) -> list[StoredSignal]:
        try:
            blobs = self._bucket.list_blobs(prefix=self._prefix)
            result: list[StoredSignal] = []
            for blob in blobs:
                if not blob.name.endswith(".json"):
                    continue
                result.append(_stored(blob.download_as_bytes(), int(blob.generation or 0)))
            return result
        except GoogleAPIError as error:
            raise _store_unavailable() from error

    def replace(self, record: AgentSignalRecord, *, generation: int) -> None:
        try:
            self._blob(record.signal_id).upload_from_string(
                _serialize(record),
                content_type="application/json; charset=utf-8",
                if_generation_match=generation,
            )
        except PreconditionFailed as error:
            raise AgentSignalObjectConflict from error
        except GoogleAPIError as error:
            raise _store_unavailable() from error

    def delete(self, signal_id: str, *, generation: int) -> None:
        try:
            self._blob(signal_id).delete(if_generation_match=generation)
        except (NotFound, PreconditionFailed):
            return
        except GoogleAPIError as error:
            raise _store_unavailable() from error


class AgentSignalInbox:
    def __init__(
        self,
        backend: AgentSignalObjectBackend,
        *,
        claim_lease_seconds: int = DEFAULT_CLAIM_LEASE_SECONDS,
        completed_retention_days: int = DEFAULT_COMPLETED_RETENTION_DAYS,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self._backend = backend
        self._claim_lease = timedelta(seconds=claim_lease_seconds)
        self._retention = timedelta(days=completed_retention_days)
        self._clock = clock

    def receive(
        self,
        payload: AgentSignalPayload,
        *,
        message_id: str,
        publish_time: datetime | None,
        attributes: dict[str, str],
    ) -> AgentSignalDeliveryResult:
        record = AgentSignalRecord.from_delivery(
            payload,
            message_id=message_id,
            publish_time=publish_time,
            attributes=attributes,
            now=self._clock(),
        )
        created = self._backend.create(record)
        existing = record if created else self._backend.read(payload.signal_id).record
        return AgentSignalDeliveryResult(
            signal_id=existing.signal_id,
            signal_type=existing.signal_type,
            created=created,
            status=existing.status,
        )

    def get(self, signal_id: str) -> AgentSignalRecord:
        return self._normalize_expired(self._backend.read(signal_id)).record

    def list(
        self,
        *,
        status: SignalStatus | None = SignalStatus.PENDING,
        priority: SignalPriority | None = None,
        signal_type: str | None = None,
        source: str | None = None,
        limit: int = 25,
    ) -> list[AgentSignalRecord]:
        if limit < 1 or limit > MAX_LIST_LIMIT:
            raise WorkspaceAdapterError(
                "agent_signal_limit_invalid", "Agent Signal limit must be between 1 and 100.", 422
            )
        self.cleanup()
        records = [self._normalize_expired(item).record for item in self._backend.list()]
        records = [
            item
            for item in records
            if (status is None or item.status == status)
            and (priority is None or item.priority == priority)
            and (signal_type is None or item.signal_type == signal_type)
            and (source is None or item.source == source)
        ]
        records.sort(
            key=lambda item: (
                0 if item.priority == SignalPriority.URGENT else 1,
                item.occurred_at,
                item.received_at,
            )
        )
        return records[:limit]

    def claim(self, signal_id: str, *, principal_id: str) -> AgentSignalRecord:
        now = self._clock()

        def transition(record: AgentSignalRecord) -> AgentSignalRecord:
            if record.status == SignalStatus.CLAIMED and not self._is_expired(record, now):
                raise WorkspaceAdapterError(
                    "agent_signal_already_claimed", "The Agent Signal is already claimed.", 409
                )
            if record.status not in {SignalStatus.PENDING, SignalStatus.CLAIMED}:
                raise _transition_error(record.status, SignalStatus.CLAIMED)
            return record.model_copy(
                update={
                    "status": SignalStatus.CLAIMED,
                    "claimed_by": principal_id,
                    "claimed_at": now,
                    "claim_expires_at": now + self._claim_lease,
                    "updated_at": now,
                }
            )

        return self._update(signal_id, transition)

    def complete(
        self,
        signal_id: str,
        *,
        completion_summary: str,
        outcome_metadata: dict[str, Any] | None = None,
    ) -> AgentSignalRecord:
        summary = completion_summary.strip()
        if not summary or len(summary) > 1000:
            raise WorkspaceAdapterError(
                "completion_summary_invalid", "A brief completion summary is required.", 422
            )
        metadata = outcome_metadata or {}
        _validate_safe_metadata(metadata)
        now = self._clock()

        def transition(record: AgentSignalRecord) -> AgentSignalRecord:
            if record.status != SignalStatus.CLAIMED or self._is_expired(record, now):
                raise _transition_error(record.status, SignalStatus.COMPLETED)
            return record.model_copy(
                update={
                    "status": SignalStatus.COMPLETED,
                    "completed_at": now,
                    "completion_summary": summary,
                    "outcome_metadata": metadata,
                    "updated_at": now,
                }
            )

        return self._update(signal_id, transition)

    def dismiss(self, signal_id: str, *, reason: str) -> AgentSignalRecord:
        normalized = reason.strip()
        if not normalized or len(normalized) > 500:
            raise WorkspaceAdapterError(
                "dismissal_reason_invalid", "A brief dismissal reason is required.", 422
            )
        now = self._clock()

        def transition(record: AgentSignalRecord) -> AgentSignalRecord:
            if record.status not in {SignalStatus.PENDING, SignalStatus.CLAIMED}:
                raise _transition_error(record.status, SignalStatus.DISMISSED)
            return record.model_copy(
                update={
                    "status": SignalStatus.DISMISSED,
                    "dismissed_at": now,
                    "dismissal_reason": normalized,
                    "updated_at": now,
                }
            )

        return self._update(signal_id, transition)

    def release(self, signal_id: str) -> AgentSignalRecord:
        now = self._clock()

        def transition(record: AgentSignalRecord) -> AgentSignalRecord:
            if record.status != SignalStatus.CLAIMED:
                raise _transition_error(record.status, SignalStatus.PENDING)
            return record.model_copy(
                update={
                    "status": SignalStatus.PENDING,
                    "claimed_by": None,
                    "claimed_at": None,
                    "claim_expires_at": None,
                    "updated_at": now,
                }
            )

        return self._update(signal_id, transition)

    def status(self, *, push_configured: bool) -> AgentSignalStatusResult:
        self.cleanup()
        records = [self._normalize_expired(item).record for item in self._backend.list()]
        return AgentSignalStatusResult(
            configured=True,
            pubsub_push_configured=push_configured,
            inbox_available=True,
            pending_count=sum(item.status == SignalStatus.PENDING for item in records),
            urgent_count=sum(
                item.status == SignalStatus.PENDING and item.priority == SignalPriority.URGENT
                for item in records
            ),
            claimed_count=sum(item.status == SignalStatus.CLAIMED for item in records),
        )

    def cleanup(self) -> int:
        cutoff = self._clock() - self._retention
        removed = 0
        for item in self._backend.list():
            record = item.record
            terminal_at = record.completed_at or record.dismissed_at
            if (
                record.status in {SignalStatus.COMPLETED, SignalStatus.DISMISSED}
                and terminal_at
                and terminal_at < cutoff
            ):
                self._backend.delete(record.signal_id, generation=item.generation)
                removed += 1
        return removed

    def _normalize_expired(self, stored: StoredSignal) -> StoredSignal:
        now = self._clock()
        if not self._is_expired(stored.record, now):
            return stored
        updated = stored.record.model_copy(
            update={
                "status": SignalStatus.PENDING,
                "claimed_by": None,
                "claimed_at": None,
                "claim_expires_at": None,
                "updated_at": now,
            }
        )
        try:
            self._backend.replace(updated, generation=stored.generation)
            return self._backend.read(updated.signal_id)
        except AgentSignalObjectConflict:
            return self._backend.read(updated.signal_id)

    @staticmethod
    def _is_expired(record: AgentSignalRecord, now: datetime) -> bool:
        return bool(
            record.status == SignalStatus.CLAIMED
            and record.claim_expires_at
            and record.claim_expires_at <= now
        )

    def _update(
        self,
        signal_id: str,
        transition: Callable[[AgentSignalRecord], AgentSignalRecord],
    ) -> AgentSignalRecord:
        for _attempt in range(MAX_WRITE_ATTEMPTS):
            stored = self._backend.read(signal_id)
            updated = transition(stored.record)
            try:
                self._backend.replace(updated, generation=stored.generation)
                return updated
            except AgentSignalObjectConflict:
                continue
        raise WorkspaceAdapterError(
            "agent_signal_conflict", "The Agent Signal changed concurrently; retry.", 409
        )


def _serialize(record: AgentSignalRecord) -> str:
    return record.model_dump_json()


def _stored(raw: bytes, generation: int) -> StoredSignal:
    try:
        return StoredSignal(
            record=AgentSignalRecord.model_validate_json(raw), generation=generation
        )
    except ValidationError as error:
        raise WorkspaceAdapterError(
            "agent_signal_store_invalid", "Agent Signal storage contains invalid data.", 503
        ) from error


def _validate_safe_metadata(metadata: dict[str, Any]) -> None:
    try:
        encoded = json.dumps(metadata, separators=(",", ":"), allow_nan=False).encode()
    except (TypeError, ValueError) as error:
        raise WorkspaceAdapterError(
            "outcome_metadata_invalid", "Outcome metadata must be JSON-safe.", 422
        ) from error
    if len(encoded) > 4096:
        raise WorkspaceAdapterError(
            "outcome_metadata_invalid", "Outcome metadata is too large.", 422
        )


def _transition_error(current: SignalStatus, target: SignalStatus) -> WorkspaceAdapterError:
    return WorkspaceAdapterError(
        "agent_signal_transition_invalid",
        f"Agent Signal cannot transition from {current.value} to {target.value}.",
        409,
    )


def _store_unavailable() -> WorkspaceAdapterError:
    return WorkspaceAdapterError(
        "agent_signal_store_unavailable", "Agent Signal Inbox storage is unavailable.", 503
    )
