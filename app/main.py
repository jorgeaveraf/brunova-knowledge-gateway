from typing import Annotated

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import JSONResponse

from app.adapters.google_workspace.docs import GoogleDocsAdapter
from app.adapters.google_workspace.drive import GoogleWorkspaceAdapter
from app.adapters.google_workspace.errors import WorkspaceAdapterError
from app.adapters.google_workspace.models import (
    DriveListResponse,
    GoogleDocContent,
    SheetRangeContent,
    WorkspaceStatusResponse,
)
from app.adapters.google_workspace.sheets import GoogleSheetsAdapter
from app.config.settings import Settings, get_settings
from app.policies.workspace import ContentReadPolicy, DriveReadPolicy

app = FastAPI(
    title="Brunova Knowledge Gateway",
    version="0.3.0"
)


def _get_valid_settings() -> Settings:
    try:
        return get_settings()
    except ValueError as error:
        raise WorkspaceAdapterError(
            "configuration_invalid",
            str(error),
            503,
        ) from error


def get_workspace_adapter() -> GoogleWorkspaceAdapter:
    return GoogleWorkspaceAdapter(_get_valid_settings())


def get_docs_adapter() -> GoogleDocsAdapter:
    return GoogleDocsAdapter(_get_valid_settings())


def get_sheets_adapter() -> GoogleSheetsAdapter:
    return GoogleSheetsAdapter(_get_valid_settings())


WorkspaceAdapter = Annotated[GoogleWorkspaceAdapter, Depends(get_workspace_adapter)]
DocsAdapter = Annotated[GoogleDocsAdapter, Depends(get_docs_adapter)]
SheetsAdapter = Annotated[GoogleSheetsAdapter, Depends(get_sheets_adapter)]


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


@app.get("/workspace/docs/{document_id}", response_model=GoogleDocContent)
def workspace_document(
    document_id: str,
    adapter: DocsAdapter,
) -> GoogleDocContent:
    safe_id = ContentReadPolicy.validate_resource_id(document_id)
    ContentReadPolicy.validate_document_limit(adapter.max_chars)
    return adapter.get_document(safe_id, max_chars=adapter.max_chars)


@app.get("/workspace/sheets/{spreadsheet_id}", response_model=SheetRangeContent)
def workspace_sheet_range(
    spreadsheet_id: str,
    adapter: SheetsAdapter,
    range_: Annotated[str, Query(alias="range", min_length=1, max_length=500)],
) -> SheetRangeContent:
    safe_id = ContentReadPolicy.validate_resource_id(spreadsheet_id)
    safe_range = ContentReadPolicy.validate_sheet_range(
        range_, max_cells=adapter.max_cells
    )
    return adapter.get_range(safe_id, range_name=safe_range)
