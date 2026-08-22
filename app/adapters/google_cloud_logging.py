"""Read-only adapter for safe Gateway operation history in Cloud Logging."""

from __future__ import annotations

import os
from typing import Any

import google.auth
from google.auth.exceptions import DefaultCredentialsError, GoogleAuthError
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.adapters.google_workspace.errors import WorkspaceAdapterError
from app.operation_history import (
    GovernedOperation,
    OperationHistoryEntry,
    OperationHistoryStore,
    OperationResult,
)

LOGGING_READ_SCOPE = "https://www.googleapis.com/auth/logging.read"
SERVICE_NAME = "brunova-knowledge-gateway"
MUTATION_ACTIONS = tuple(operation.value for operation in GovernedOperation)


class CloudLoggingOperationHistoryStore(OperationHistoryStore):
    """Query Cloud Logging but expose only an allowlisted public model."""

    def __init__(self, *, service: Any | None = None, project_id: str = "") -> None:
        self._service = service
        self._project_id = project_id.strip()

    def list(
        self,
        *,
        source_id: str | None,
        operation: GovernedOperation | None,
        limit: int,
    ) -> list[OperationHistoryEntry]:
        try:
            service, project_id = self._client()
            response = (
                service.entries()
                .list(
                    body={
                        "resourceNames": [f"projects/{project_id}"],
                        "filter": self._filter(source_id, operation),
                        "orderBy": "timestamp desc",
                        "pageSize": limit,
                    }
                )
                .execute()
            )
            return self._safe_entries(response.get("entries", ()))
        except WorkspaceAdapterError:
            raise
        except Exception as error:
            raise self._map_error(error) from error

    def _client(self) -> tuple[Any, str]:
        if self._service is not None:
            project_id = self._project_id or "test-project"
            return self._service, project_id
        credentials, detected_project = google.auth.default(
            scopes=(LOGGING_READ_SCOPE,)
        )
        project_id = (
            self._project_id
            or os.getenv("GOOGLE_CLOUD_PROJECT", "").strip()
            or detected_project
            or ""
        )
        if not project_id:
            raise WorkspaceAdapterError(
                "operation_history_unavailable",
                "The runtime GCP project could not be determined.",
                503,
            )
        return (
            build("logging", "v2", credentials=credentials, cache_discovery=False),
            project_id,
        )

    @staticmethod
    def _filter(
        source_id: str | None,
        operation: GovernedOperation | None,
    ) -> str:
        action_filter = (
            f'jsonPayload.action="{operation.value}"'
            if operation
            else "(" + " OR ".join(
                f'jsonPayload.action="{action}"' for action in MUTATION_ACTIONS
            ) + ")"
        )
        filters = [
            'resource.type="cloud_run_revision"',
            f'resource.labels.service_name="{SERVICE_NAME}"',
            f'jsonPayload.service="{SERVICE_NAME}"',
            action_filter,
        ]
        if source_id:
            filters.append(f'jsonPayload.source_id="{source_id}"')
        return " AND ".join(filters)

    @staticmethod
    def _safe_entries(
        entries: list[dict[str, Any]] | tuple[Any, ...],
    ) -> list[OperationHistoryEntry]:
        safe: list[OperationHistoryEntry] = []
        for raw_entry in entries:
            payload = raw_entry.get("jsonPayload") or {}
            request_id = payload.get("request_id")
            source_id = payload.get("source_id")
            timestamp = raw_entry.get("timestamp") or payload.get("timestamp")
            try:
                if (
                    not isinstance(request_id, str)
                    or not isinstance(source_id, str)
                    or not isinstance(timestamp, str)
                    or not timestamp
                ):
                    continue
                safe.append(
                    OperationHistoryEntry(
                        timestamp=timestamp,
                        operation=GovernedOperation(payload.get("action")),
                        source_id=source_id,
                        result=OperationResult(payload.get("result")),
                        approval_reference=(
                            payload.get("approval_reference")
                            if isinstance(payload.get("approval_reference"), str)
                            else None
                        ),
                        request_id=request_id,
                        correlation_id=(
                            payload.get("correlation_id")
                            if isinstance(payload.get("correlation_id"), str)
                            else request_id
                        ),
                    )
                )
            except (TypeError, ValueError):
                continue
        return safe

    @staticmethod
    def _map_error(error: Exception) -> WorkspaceAdapterError:
        if isinstance(error, DefaultCredentialsError):
            return WorkspaceAdapterError(
                "credentials_unavailable",
                "Application Default Credentials are not available to the runtime.",
                503,
            )
        if isinstance(error, HttpError):
            status = getattr(error.resp, "status", None)
            if status in (401, 403):
                return WorkspaceAdapterError(
                    "operation_history_permission_denied",
                    "The runtime identity cannot read Gateway audit history.",
                    403,
                )
            if status == 429:
                return WorkspaceAdapterError(
                    "operation_history_rate_limited",
                    "Operation history is temporarily rate limited.",
                    503,
                )
        return WorkspaceAdapterError(
            "operation_history_unavailable",
            "Operation history is temporarily unavailable.",
            503,
        )
