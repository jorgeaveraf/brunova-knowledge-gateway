"""Public response models for the Google Workspace adapter."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Optional

from pydantic import BaseModel

from app.source_registry import Classification

SERVICE_NAME = "brunova-knowledge-gateway"


class WorkspaceConnection(BaseModel):
    connected: bool
    delegated_user: str


class WorkspaceStatusResponse(BaseModel):
    service: str = SERVICE_NAME
    workspace: WorkspaceConnection
    request_id: str

    @classmethod
    def connected(
        cls, delegated_user: str, request_id: str
    ) -> "WorkspaceStatusResponse":
        return cls(
            workspace=WorkspaceConnection(
                connected=True,
                delegated_user=delegated_user,
            ),
            request_id=request_id,
        )


class SourceMetadata(BaseModel):
    id: str
    name: str
    classification: Classification


class DriveFile(BaseModel):
    id: str
    name: str
    type: str
    source: SourceMetadata


class DriveListResponse(BaseModel):
    files: list[DriveFile]
    request_id: str


class GoogleDocContent(BaseModel):
    id: str
    name: str
    mime_type: str
    modified_time: str
    text: str
    truncated: bool
    limit: int
    request_id: Optional[str] = None
    source: Optional[SourceMetadata] = None


class SheetRangeContent(BaseModel):
    spreadsheet_id: str
    range: str
    values: list[list[object]]
    request_id: Optional[str] = None
    source: Optional[SourceMetadata] = None


class SourceArtifact(BaseModel):
    id: str
    name: str
    type: Literal["document"]


class SourceArtifactMutationResult(BaseModel):
    artifact: SourceArtifact
    source: SourceMetadata
    status: Literal["created", "updated", "moved"]
    request_id: Optional[str] = None


@dataclass(frozen=True)
class WorkspaceResource:
    id: str
    name: str
    mime_type: str
    modified_time: str
    drive_id: Optional[str]
    ancestor_ids: tuple[str, ...]
    parent_ids: tuple[str, ...] = ()
