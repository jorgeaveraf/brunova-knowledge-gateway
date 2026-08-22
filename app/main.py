from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.exceptions import RequestValidationError
from fastapi.encoders import jsonable_encoder
from fastapi.responses import JSONResponse

from app.audit import correlation_id, emit_audit_event, request_audit_context
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
from app.policies.source_access import SourceAccessPolicy

app = FastAPI(
    title="Brunova Knowledge Gateway",
    version="0.4.0"
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


def get_source_policy() -> SourceAccessPolicy:
    try:
        return SourceAccessPolicy(_get_valid_settings())
    except ValueError as error:
        raise WorkspaceAdapterError("configuration_invalid", str(error), 503) from error


WorkspaceAdapter = Annotated[GoogleWorkspaceAdapter, Depends(get_workspace_adapter)]
DocsAdapter = Annotated[GoogleDocsAdapter, Depends(get_docs_adapter)]
SheetsAdapter = Annotated[GoogleSheetsAdapter, Depends(get_sheets_adapter)]
SourcePolicy = Annotated[SourceAccessPolicy, Depends(get_source_policy)]


@app.middleware("http")
async def correlation_and_audit(request: Request, call_next):
    request.state.request_id = correlation_id(request.headers.get("X-Correlation-ID"))
    try:
        response = await call_next(request)
    except Exception:
        action, resource_id, resource_type = request_audit_context(request)
        if action:
            emit_audit_event(
                request,
                action=action,
                resource_id=resource_id,
                resource_type=resource_type,
                result="error",
                http_status=500,
                error_code="unhandled_error",
            )
        raise
    response.headers["X-Correlation-ID"] = request.state.request_id
    action, resource_id, resource_type = request_audit_context(request)
    if action:
        result = (
            "success"
            if response.status_code < 400
            else "rejected"
            if response.status_code < 500
            else "error"
        )
        emit_audit_event(
            request,
            action=action,
            resource_id=resource_id,
            resource_type=resource_type,
            result=result,
            http_status=response.status_code,
            error_code=getattr(request.state, "error_code", None),
        )
    return response


@app.exception_handler(WorkspaceAdapterError)
async def workspace_adapter_error_handler(
    request: Request, error: WorkspaceAdapterError
) -> JSONResponse:
    request.state.error_code = error.code
    return JSONResponse(
        status_code=error.status_code,
        content={
            "error": {"code": error.code, "message": error.message},
            "request_id": request.state.request_id,
        },
    )


@app.exception_handler(HTTPException)
async def http_error_handler(request: Request, error: HTTPException) -> JSONResponse:
    request.state.error_code = "policy_validation_failed"
    return JSONResponse(
        status_code=error.status_code,
        content={
            "detail": jsonable_encoder(error.detail),
            "request_id": request.state.request_id,
        },
    )


@app.exception_handler(RequestValidationError)
async def request_validation_error_handler(
    request: Request, error: RequestValidationError
) -> JSONResponse:
    request.state.error_code = "invalid_request"
    return JSONResponse(
        status_code=422,
        content={
            "detail": jsonable_encoder(error.errors()),
            "request_id": request.state.request_id,
        },
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
def workspace_status(request: Request, adapter: WorkspaceAdapter) -> WorkspaceStatusResponse:
    adapter.check_connection()
    return WorkspaceStatusResponse.connected(
        adapter.delegated_user, request.state.request_id
    )


@app.get("/workspace/drive/list", response_model=DriveListResponse)
def workspace_drive_list(
    request: Request,
    adapter: WorkspaceAdapter,
    source_policy: SourcePolicy,
    limit: Annotated[int, Query(ge=1)] = 10,
) -> DriveListResponse:
    safe_limit = DriveReadPolicy.validate_list_limit(limit)
    return DriveListResponse(
        files=adapter.list_files(limit=safe_limit, source_policy=source_policy),
        request_id=request.state.request_id,
    )


@app.get("/workspace/docs/{document_id}", response_model=GoogleDocContent)
def workspace_document(
    request: Request,
    document_id: str,
    workspace_adapter: WorkspaceAdapter,
    adapter: DocsAdapter,
    source_policy: SourcePolicy,
) -> GoogleDocContent:
    safe_id = ContentReadPolicy.validate_resource_id(document_id)
    ContentReadPolicy.validate_document_limit(adapter.max_chars)
    resource = workspace_adapter.get_resource(safe_id)
    source_policy.authorize(resource)
    result = adapter.get_document(resource, max_chars=adapter.max_chars)
    return result.model_copy(update={"request_id": request.state.request_id})


@app.get("/workspace/sheets/{spreadsheet_id}", response_model=SheetRangeContent)
def workspace_sheet_range(
    request: Request,
    spreadsheet_id: str,
    workspace_adapter: WorkspaceAdapter,
    adapter: SheetsAdapter,
    source_policy: SourcePolicy,
    range_: Annotated[str, Query(alias="range", min_length=1, max_length=500)],
) -> SheetRangeContent:
    safe_id = ContentReadPolicy.validate_resource_id(spreadsheet_id)
    safe_range = ContentReadPolicy.validate_sheet_range(
        range_, max_cells=adapter.max_cells
    )
    resource = workspace_adapter.get_resource(safe_id)
    source_policy.authorize(resource)
    result = adapter.get_range(resource, range_name=safe_range)
    return result.model_copy(update={"request_id": request.state.request_id})
