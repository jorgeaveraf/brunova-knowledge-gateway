"""Safe operational-history contract backed by structured Gateway audit records."""

from __future__ import annotations

from enum import Enum
from typing import Protocol

from pydantic import BaseModel, ConfigDict

from app.adapters.google_workspace.errors import WorkspaceAdapterError
from app.policies.source_access import SourceAccessPolicy
from app.source_registry import SourceRegistry

DEFAULT_OPERATION_HISTORY_LIMIT = 10
MAX_OPERATION_HISTORY_LIMIT = 50


class GovernedOperation(str, Enum):
    CREATE_SOURCE_ARTIFACT = "create_source_artifact"
    UPDATE_SOURCE_ARTIFACT = "update_source_artifact"
    MOVE_SOURCE_ARTIFACT = "move_source_artifact"


class OperationResult(str, Enum):
    SUCCESS = "success"
    REJECTED = "rejected"
    ERROR = "error"


class OperationHistoryEntry(BaseModel):
    """Allowlisted audit fields safe for authorized MCP consumers."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    timestamp: str
    operation: GovernedOperation
    source_id: str
    result: OperationResult
    approval_reference: str | None = None
    request_id: str
    correlation_id: str


class OperationHistoryStore(Protocol):
    def list(
        self,
        *,
        source_id: str | None,
        operation: GovernedOperation | None,
        limit: int,
    ) -> list[OperationHistoryEntry]: ...


def list_authorized_operation_history(
    *,
    store: OperationHistoryStore,
    registry: SourceRegistry,
    source_policy: SourceAccessPolicy,
    source_id: str | None = None,
    operation: GovernedOperation | None = None,
    limit: int = DEFAULT_OPERATION_HISTORY_LIMIT,
) -> list[OperationHistoryEntry]:
    """Return only events for sources still authorized by current policy."""

    if limit < 1 or limit > MAX_OPERATION_HISTORY_LIMIT:
        raise WorkspaceAdapterError(
            "operation_history_limit_invalid",
            f"Operation history limit must be between 1 and {MAX_OPERATION_HISTORY_LIMIT}.",
            422,
        )

    if source_id is not None:
        try:
            selected = registry.get(source_id)
        except KeyError as error:
            raise WorkspaceAdapterError(
                "source_not_found",
                "The requested knowledge source is not registered.",
                404,
            ) from error
        source_policy.authorize_source(selected, require_read=False)
        allowed_source_ids = {source_id}
    else:
        allowed_source_ids: set[str] = set()
        for source in registry.sources:
            try:
                source_policy.authorize_source(source, require_read=False)
            except WorkspaceAdapterError:
                continue
            allowed_source_ids.add(source.id)

    if not allowed_source_ids:
        return []

    # Fetch a bounded surplus so stale or subsequently disabled sources can be
    # removed without exposing them. The public result remains capped by limit.
    fetch_limit = min(limit * 5, MAX_OPERATION_HISTORY_LIMIT * 5)
    entries = store.list(
        source_id=source_id,
        operation=operation,
        limit=fetch_limit,
    )
    return [
        entry for entry in entries if entry.source_id in allowed_source_ids
    ][:limit]
