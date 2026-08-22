"""Governed MCP interface backed exclusively by Knowledge Gateway operations."""

import os
from collections.abc import Callable
from typing import TypeVar

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel

from app.adapters.google_workspace.errors import WorkspaceAdapterError
from app.adapters.google_workspace.models import (
    DriveFile,
    GoogleDocContent,
    SheetRangeContent,
)
from app.audit import correlation_id, emit_audit_record
from app.knowledge import (
    discover_candidate_sources,
    list_authorized_source_files,
    list_registered_sources,
    registered_source,
    retrieve_authorized_document,
    retrieve_authorized_sheet_range,
)
from app.runtime import KnowledgeRuntime, get_runtime_gateway
from app.source_registry import SourceRegistryMetadata
from app.source_discovery.interface import DiscoveryResponse

T = TypeVar("T")
MCP_DOCUMENT_RESULT_LIMIT = 20


class SourceListToolResult(BaseModel):
    sources: list[SourceRegistryMetadata]
    request_id: str


class DocumentListToolResult(BaseModel):
    documents: list[DriveFile]
    request_id: str


class SourceDiscoveryToolResult(DiscoveryResponse):
    request_id: str


mcp_server = MCPServer(
    name="brunova-knowledge-gateway",
    version="0.10.1",
    instructions=(
        "Read authorized Brunova knowledge through semantic sources. "
        "All tools are read-only and policy governed."
    ),
)


def _mcp_request_id(ctx: Context) -> str:
    return correlation_id(str(ctx.request_id))


def _execute_tool(
    *,
    ctx: Context,
    action: str,
    operation: Callable[[KnowledgeRuntime, str], T],
    source_id: str | None = None,
    resource_id: str | None = None,
    resource_type: str | None = None,
    candidate_count: Callable[[T], int] | None = None,
) -> T:
    request_id = _mcp_request_id(ctx)
    source_classification: str | None = None
    try:
        runtime = get_runtime_gateway()
        if source_id:
            source = registered_source(runtime.registry, source_id)
            source_classification = source.classification.value
        result = operation(runtime, request_id)
        emit_audit_record(
            request_id=request_id,
            action=action,
            resource_id=resource_id,
            resource_type=resource_type,
            result="success",
            http_status=200,
            source_id=source_id,
            source_classification=source_classification,
            candidate_count=(candidate_count(result) if candidate_count else None),
        )
        return result
    except WorkspaceAdapterError as error:
        emit_audit_record(
            request_id=request_id,
            action=action,
            resource_id=resource_id,
            resource_type=resource_type,
            result="rejected" if error.status_code < 500 else "error",
            http_status=error.status_code,
            error_code=error.code,
            source_id=source_id,
            source_classification=source_classification,
        )
        raise RuntimeError(f"{error.code}: {error.message}") from error
    except Exception:
        emit_audit_record(
            request_id=request_id,
            action=action,
            resource_id=resource_id,
            resource_type=resource_type,
            result="error",
            http_status=500,
            error_code="unhandled_error",
            source_id=source_id,
            source_classification=source_classification,
        )
        raise


@mcp_server.tool()
def list_sources(ctx: Context) -> SourceListToolResult:
    """List non-sensitive metadata for Brunova knowledge sources."""

    return _execute_tool(
        ctx=ctx,
        action="list_sources",
        operation=lambda runtime, request_id: SourceListToolResult(
            sources=list_registered_sources(runtime.registry),
            request_id=request_id,
        ),
        resource_type="source_registry",
    )


@mcp_server.tool()
def discover_source_candidates(
    ctx: Context,
    limit: int = 25,
) -> SourceDiscoveryToolResult:
    """Propose unregistered Shared Drives and root folders for human review."""

    def operation(
        runtime: KnowledgeRuntime,
        request_id: str,
    ) -> SourceDiscoveryToolResult:
        result = discover_candidate_sources(
            source_discovery=runtime.source_discovery,
            limit=limit,
        )
        response = DiscoveryResponse.from_result(result)
        return SourceDiscoveryToolResult(
            **response.model_dump(),
            request_id=request_id,
        )

    return _execute_tool(
        ctx=ctx,
        action="discover_source_candidates",
        operation=operation,
        resource_type="source_discovery",
        candidate_count=lambda result: len(result.candidates),
    )


@mcp_server.tool()
def list_source_documents(
    source_id: str,
    ctx: Context,
    query: str | None = None,
) -> DocumentListToolResult:
    """List authorized documents in one source, optionally filtering by name."""

    def operation(runtime: KnowledgeRuntime, request_id: str) -> DocumentListToolResult:
        _, files = list_authorized_source_files(
            registry=runtime.registry,
            source_policy=runtime.source_policy,
            workspace_adapter=runtime.workspace_adapter,
            source_id=source_id,
            limit=100,
        )
        documents = [item for item in files if item.type == "document"]
        if query and query.strip():
            normalized_query = query.strip().casefold()
            documents = [
                item for item in documents if normalized_query in item.name.casefold()
            ]
        return DocumentListToolResult(
            documents=documents[:MCP_DOCUMENT_RESULT_LIMIT],
            request_id=request_id,
        )

    return _execute_tool(
        ctx=ctx,
        action="list_source_documents",
        operation=operation,
        source_id=source_id,
        resource_type="google_drive",
    )


@mcp_server.tool()
def retrieve_document(
    source_id: str,
    document_id: str,
    ctx: Context,
) -> GoogleDocContent:
    """Retrieve an authorized document that belongs to the selected source."""

    def operation(runtime: KnowledgeRuntime, request_id: str) -> GoogleDocContent:
        _, document = retrieve_authorized_document(
            registry=runtime.registry,
            source_policy=runtime.source_policy,
            workspace_adapter=runtime.workspace_adapter,
            docs_adapter=runtime.docs_adapter,
            source_id=source_id,
            document_id=document_id,
        )
        return document.model_copy(update={"request_id": request_id})

    return _execute_tool(
        ctx=ctx,
        action="retrieve_document",
        operation=operation,
        source_id=source_id,
        resource_id=document_id,
        resource_type="google_doc",
    )


@mcp_server.tool()
def retrieve_sheet_range(
    source_id: str,
    spreadsheet_id: str,
    range: str,
    ctx: Context,
) -> SheetRangeContent:
    """Retrieve a bounded range from a sheet in the selected source."""

    def operation(runtime: KnowledgeRuntime, request_id: str) -> SheetRangeContent:
        _, sheet = retrieve_authorized_sheet_range(
            registry=runtime.registry,
            source_policy=runtime.source_policy,
            workspace_adapter=runtime.workspace_adapter,
            sheets_adapter=runtime.sheets_adapter,
            source_id=source_id,
            spreadsheet_id=spreadsheet_id,
            range_name=range,
        )
        return sheet.model_copy(update={"request_id": request_id})

    return _execute_tool(
        ctx=ctx,
        action="retrieve_sheet_range",
        operation=operation,
        source_id=source_id,
        resource_id=spreadsheet_id,
        resource_type="google_sheet",
    )


def _csv_environment(name: str, default: str = "") -> list[str]:
    return [item.strip() for item in os.getenv(name, default).split(",") if item.strip()]


transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=_csv_environment(
        "MCP_ALLOWED_HOSTS",
        "localhost:*,127.0.0.1:*",
    ),
    allowed_origins=_csv_environment("MCP_ALLOWED_ORIGINS"),
)

mcp_http_app = mcp_server.streamable_http_app(
    streamable_http_path="/",
    stateless_http=True,
    json_response=True,
    transport_security=transport_security,
    host="0.0.0.0",
)
