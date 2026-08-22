from typing import Annotated

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse

from app.adapters.google_workspace.drive import GoogleWorkspaceAdapter
from app.adapters.google_workspace.errors import WorkspaceAdapterError
from app.adapters.google_workspace.models import DriveListResponse, WorkspaceStatusResponse
from app.config.settings import get_settings
from app.policies.workspace import DriveReadPolicy

app = FastAPI(
    title="Brunova Knowledge Gateway",
    version="0.2.0"
)


def get_workspace_adapter() -> GoogleWorkspaceAdapter:
    try:
        return GoogleWorkspaceAdapter(get_settings())
    except ValueError as error:
        raise WorkspaceAdapterError(
            "configuration_invalid",
            str(error),
            503,
        ) from error


WorkspaceAdapter = Annotated[GoogleWorkspaceAdapter, Depends(get_workspace_adapter)]


@app.exception_handler(WorkspaceAdapterError)
async def workspace_adapter_error_handler(
    _request: Request, error: WorkspaceAdapterError
) -> JSONResponse:
    return JSONResponse(
        status_code=error.status_code,
        content={"error": {"code": error.code, "message": error.message}},
    )


@app.get("/health")
def health():
    return {
        "status": "ok",
        "service": "brunova-knowledge-gateway"
    }


@app.get("/identity")
def identity():
    return {
        "service_account": "brunova-knowledge-agent",
        "environment": "cloud-run"
    }


@app.get("/capabilities")
def capabilities():
    return {
        "capabilities": [
            "google_workspace"
        ]
    }


@app.get("/workspace/status", response_model=WorkspaceStatusResponse)
def workspace_status(adapter: WorkspaceAdapter) -> WorkspaceStatusResponse:
    adapter.check_connection()
    return WorkspaceStatusResponse.connected(adapter.delegated_user)


@app.get("/workspace/drive/list", response_model=DriveListResponse)
def workspace_drive_list(
    adapter: WorkspaceAdapter,
    limit: Annotated[int, Query(ge=1)] = 10,
) -> DriveListResponse:
    safe_limit = DriveReadPolicy.validate_list_limit(limit)
    return DriveListResponse(files=adapter.list_files(limit=safe_limit))
