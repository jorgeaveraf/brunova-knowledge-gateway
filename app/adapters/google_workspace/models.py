"""Public response models for the Google Workspace adapter."""

from pydantic import BaseModel

SERVICE_NAME = "brunova-knowledge-gateway"


class WorkspaceConnection(BaseModel):
    connected: bool
    delegated_user: str


class WorkspaceStatusResponse(BaseModel):
    service: str = SERVICE_NAME
    workspace: WorkspaceConnection

    @classmethod
    def connected(cls, delegated_user: str) -> "WorkspaceStatusResponse":
        return cls(
            workspace=WorkspaceConnection(
                connected=True,
                delegated_user=delegated_user,
            )
        )


class DriveFile(BaseModel):
    name: str
    type: str


class DriveListResponse(BaseModel):
    files: list[DriveFile]
