"""Governed MCP interface backed exclusively by Knowledge Gateway operations."""

import os
import time
from collections.abc import Callable
from typing import Any, Literal, TypeVar

from mcp.server import MCPServer
from mcp.server.mcpserver import Context
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import CallToolResult, TextContent, Tool as MCPTool
from pydantic import BaseModel

from app.adapters.google_workspace.errors import WorkspaceAdapterError
from app.adapters.google_workspace.models import (
    ArtifactConversionTarget,
    ArtifactMetadata,
    DriveFile,
    GoogleDocContent,
    SheetRangeContent,
    SourceArtifactMutationResult,
)
from app.adapters.hubspot.mcp_client import account_metadata_from_result
from app.adapters.hubspot.models import HubSpotToolDescriptor, HubSpotToolResult
from app.adapters.hubspot.runtime import get_hubspot_runtime
from app.adapters.n8n.models import (
    N8NStatusResult,
    N8NToolListResult,
)
from app.adapters.n8n.runtime import get_n8n_client
from app.audit import correlation_id, emit_audit_record
from app.artifact_refs import ArtifactReferenceCodec
from app.document_production import (
    ArtifactReference,
    ArtifactReferenceConversionResult,
    ArtifactReferenceMutationResult,
    DocumentEditOperation,
    DocumentEditResult,
    DocumentQualityRequirements,
    DocumentQualityResult,
    DocumentStructure,
    DocumentTabInspectionResult,
    DocumentTabMutationResult,
)
from app.policies.content_mutation import ContentMutationPolicy
from app.operation_history import (
    DEFAULT_OPERATION_HISTORY_LIMIT,
    GovernedOperation,
    OperationHistoryEntry,
    list_authorized_operation_history,
)
from app.knowledge import (
    discover_candidate_sources,
    copy_authorized_source_artifact,
    create_authorized_document_tab,
    create_authorized_source_artifact,
    convert_authorized_source_artifact,
    delete_authorized_source_artifact,
    delete_authorized_document_tab,
    get_candidate_details,
    inspect_authorized_source_artifacts,
    inspect_authorized_document_structure,
    inspect_authorized_document_tab,
    list_authorized_source_files,
    list_registered_sources,
    list_registered_source_proposals,
    move_authorized_source_artifact,
    rename_authorized_source_artifact,
    rename_authorized_document_tab,
    resolve_authorized_source_artifact,
    registered_source,
    registered_source_proposal,
    register_source_proposal,
    retrieve_authorized_document,
    retrieve_authorized_sheet_range,
    share_authorized_source_artifact,
    edit_authorized_source_document,
    update_authorized_source_artifact,
    validate_authorized_document_structure,
)
from app.runtime import KnowledgeRuntime, get_runtime_gateway
from app.source_discovery.interface import CandidateDetailsResponse, DiscoveryResponse
from app.source_governance import (
    SourceProposalDetails,
    SourceProposalReceipt,
    SourceProposalSummary,
)
from app.source_registry import Classification, SourceRegistryMetadata

T = TypeVar("T")
MCP_DOCUMENT_RESULT_LIMIT = 20


class SourceListToolResult(BaseModel):
    sources: list[SourceRegistryMetadata]
    request_id: str


class DocumentListToolResult(BaseModel):
    documents: list[DriveFile]
    request_id: str


class ArtifactInspectionToolResult(BaseModel):
    artifacts: list[ArtifactMetadata]
    request_id: str


class SourceDiscoveryToolResult(DiscoveryResponse):
    request_id: str


class CandidateDetailsToolResult(CandidateDetailsResponse):
    request_id: str


class SourceProposalToolResult(SourceProposalReceipt):
    request_id: str


class SourceProposalListToolResult(BaseModel):
    proposals: list[SourceProposalSummary]
    request_id: str


class SourceProposalDetailsToolResult(BaseModel):
    proposal: SourceProposalDetails
    request_id: str


class SourceMutationToolResult(SourceArtifactMutationResult):
    request_id: str


class ArtifactConversionToolResult(ArtifactReferenceConversionResult):
    request_id: str


class ArtifactReferenceToolResult(ArtifactReference):
    request_id: str


class ArtifactReferenceMutationToolResult(ArtifactReferenceMutationResult):
    request_id: str


class DocumentStructureToolResult(DocumentStructure):
    request_id: str


class DocumentEditToolResult(DocumentEditResult):
    request_id: str


class DocumentQualityToolResult(DocumentQualityResult):
    request_id: str


class DocumentTabInspectionToolResult(DocumentTabInspectionResult):
    request_id: str


class DocumentTabMutationToolResult(DocumentTabMutationResult):
    request_id: str


class OperationHistoryToolResult(BaseModel):
    operations: list[OperationHistoryEntry]
    request_id: str


class HubSpotToolListResult(BaseModel):
    tools: list[HubSpotToolDescriptor]
    request_id: str


class BrunovaMCPServer(MCPServer):
    """MCP server that projects the live n8n catalog without a second policy."""

    async def list_tools(self) -> list[MCPTool]:
        tools = await super().list_tools()
        try:
            downstream = await get_n8n_client().list_tools()
        except Exception:
            # Provider isolation: n8n outages never hide native/Workspace/HubSpot tools.
            return tools
        occupied = {tool.name for tool in tools}
        for descriptor in downstream:
            exposed_name = _n8n_exposed_name(descriptor.name, occupied)
            occupied.add(exposed_name)
            tools.append(MCPTool(
                name=exposed_name,
                description=descriptor.description,
                input_schema=descriptor.input_schema,
                _meta={**descriptor.metadata, "provider": "n8n", "downstream_tool": descriptor.name},
            ))
        return tools

    async def call_tool(
        self,
        name: str,
        arguments: dict[str, Any],
        context: Context | None = None,
    ) -> CallToolResult:
        if not name.startswith("n8n_") or name in {"n8n_status", "n8n_list_tools"}:
            return await super().call_tool(name, arguments, context)
        client = get_n8n_client()
        tools = await client.list_tools()
        occupied = {"n8n_status", "n8n_list_tools"}
        mapping: dict[str, str] = {}
        for descriptor in tools:
            exposed = _n8n_exposed_name(descriptor.name, occupied)
            occupied.add(exposed)
            mapping[exposed] = descriptor.name
        downstream_name = mapping.get(name)
        if downstream_name is None:
            return CallToolResult(
                content=[TextContent(type="text", text="n8n_tool_unavailable: The n8n tool is not currently exposed.")],
                is_error=True,
            )
        request_id = correlation_id(str(context.request_id) if context else None)
        approval_reference = _approval_reference(context)
        started = time.monotonic()
        try:
            result = await client.call_tool(downstream_name, arguments)
            downstream_failed = bool(result.get("isError", False)) if isinstance(result, dict) else False
            emit_audit_record(
                request_id=request_id, action="n8n_tool_call", resource_id=None,
                resource_type="n8n_mcp_tool",
                result="error" if downstream_failed else "success",
                http_status=502 if downstream_failed else 200,
                provider="n8n", tool=downstream_name,
                approval_reference=approval_reference,
                duration_ms=round((time.monotonic() - started) * 1000),
            )
            return CallToolResult.model_validate(result)
        except WorkspaceAdapterError as error:
            emit_audit_record(
                request_id=request_id, action="n8n_tool_call", resource_id=None,
                resource_type="n8n_mcp_tool",
                result="rejected" if error.status_code < 500 else "error",
                http_status=error.status_code, error_code=error.code,
                provider="n8n", tool=downstream_name,
                approval_reference=approval_reference,
                duration_ms=round((time.monotonic() - started) * 1000),
            )
            return CallToolResult(
                content=[TextContent(type="text", text=f"{error.code}: {error.message}")],
                is_error=True,
            )


def _n8n_exposed_name(downstream_name: str, occupied: set[str]) -> str:
    preferred = f"n8n_{downstream_name}"
    return f"n8n_downstream_{downstream_name}" if preferred in occupied else preferred


def _approval_reference(context: Context | None) -> str | None:
    if context is None:
        return None
    try:
        meta = context.request_context.meta
    except ValueError:
        return None
    if meta is None:
        return None
    value = meta.get("approval_reference") if hasattr(meta, "get") else getattr(meta, "approval_reference", None)
    return str(value)[:256] if value else None


mcp_server = BrunovaMCPServer(
    name="brunova-knowledge-gateway",
    version="0.21.0",
    instructions=(
        "Read authorized Brunova knowledge, manage pending source proposals, and "
        "execute full capability-gated Google Workspace CRUD operations with an "
        "external "
        "approval reference. Governed HubSpot Remote MCP read and approved mutation "
        "tools are available without exposing HubSpot credentials. Safe operation history is available without direct "
        "log access. No tool approves sources or mutates Source Registry."
    ),
)


def _reference_codec(runtime: KnowledgeRuntime) -> ArtifactReferenceCodec:
    if runtime.artifact_reference_codec is None:
        raise WorkspaceAdapterError(
            "artifact_reference_unavailable",
            "Artifact references are not configured.",
            503,
        )
    return runtime.artifact_reference_codec


def _resolved_artifact_id(
    runtime: KnowledgeRuntime,
    *,
    source_id: str,
    artifact_ref: str | None,
    legacy_id: str | None,
) -> str:
    if artifact_ref:
        return _reference_codec(runtime).decode(artifact_ref, source_id=source_id)
    if legacy_id:
        return legacy_id
    raise WorkspaceAdapterError(
        "artifact_reference_required", "An artifact reference is required.", 422
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
    proposal_id: Callable[[T], str] | None = None,
    result_resource_id: Callable[[T], str] | None = None,
    created_resource_id: Callable[[T], str] | None = None,
    approval_reference: str | None = None,
    audience: str | None = None,
    destination_source_id: str | None = None,
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
            resource_id=(
                result_resource_id(result) if result_resource_id else resource_id
            ),
            resource_type=resource_type,
            result="success",
            http_status=200,
            source_id=source_id,
            source_classification=source_classification,
            candidate_count=(candidate_count(result) if candidate_count else None),
            proposal_id=(proposal_id(result) if proposal_id else None),
            approval_reference=approval_reference,
            audience=audience,
            created_resource_id=(
                created_resource_id(result) if created_resource_id else None
            ),
            destination_source_id=destination_source_id,
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
            approval_reference=approval_reference,
            audience=audience,
            destination_source_id=destination_source_id,
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
            approval_reference=approval_reference,
            audience=audience,
            destination_source_id=destination_source_id,
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
def get_operation_history(
    ctx: Context,
    source_id: str | None = None,
    operation: GovernedOperation | None = None,
    limit: int = DEFAULT_OPERATION_HISTORY_LIMIT,
) -> OperationHistoryToolResult:
    """List safe governed-mutation audit metadata for authorized sources."""

    def operation_query(
        runtime: KnowledgeRuntime,
        request_id: str,
    ) -> OperationHistoryToolResult:
        if runtime.operation_history_store is None:
            raise WorkspaceAdapterError(
                "operation_history_unavailable",
                "Operation history is not configured.",
                503,
            )
        return OperationHistoryToolResult(
            operations=list_authorized_operation_history(
                store=runtime.operation_history_store,
                registry=runtime.registry,
                source_policy=runtime.source_policy,
                source_id=source_id,
                operation=operation,
                limit=limit,
            ),
            request_id=request_id,
        )

    return _execute_tool(
        ctx=ctx,
        action="get_operation_history",
        operation=operation_query,
        source_id=source_id,
        resource_type="operation_history",
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
def get_source_candidate_details(
    candidate_id: str,
    ctx: Context,
) -> CandidateDetailsToolResult:
    """Inspect safe details for one currently discoverable source candidate."""

    def operation(
        runtime: KnowledgeRuntime,
        request_id: str,
    ) -> CandidateDetailsToolResult:
        response = get_candidate_details(
            source_discovery=runtime.source_discovery,
            candidate_id=candidate_id,
        )
        return CandidateDetailsToolResult(
            **response.model_dump(),
            request_id=request_id,
        )

    return _execute_tool(
        ctx=ctx,
        action="get_source_candidate_details",
        operation=operation,
        resource_id=candidate_id,
        resource_type="source_candidate",
    )


@mcp_server.tool()
def create_source_proposal(
    candidate_id: str,
    name: str,
    classification: Classification,
    reason: str,
    ctx: Context,
) -> SourceProposalToolResult:
    """Register a pending intent for human review without applying any change."""

    def operation(
        runtime: KnowledgeRuntime,
        request_id: str,
    ) -> SourceProposalToolResult:
        proposal = register_source_proposal(
            source_discovery=runtime.source_discovery,
            proposal_store=runtime.proposal_store,
            candidate_id=candidate_id,
            name=name,
            classification=classification,
            reason=reason,
            request_id=request_id,
        )
        receipt = SourceProposalReceipt(
            proposal_id=proposal.proposal_id,
            status=proposal.status,
        )
        return SourceProposalToolResult(
            **receipt.model_dump(),
            request_id=request_id,
        )

    return _execute_tool(
        ctx=ctx,
        action="create_source_proposal",
        operation=operation,
        resource_id=candidate_id,
        resource_type="source_proposal",
        proposal_id=lambda result: result.proposal_id,
    )


@mcp_server.tool()
def list_source_proposals(ctx: Context) -> SourceProposalListToolResult:
    """List safe pending proposal summaries from the durable registry."""

    return _execute_tool(
        ctx=ctx,
        action="list_source_proposals",
        operation=lambda runtime, request_id: SourceProposalListToolResult(
            proposals=list_registered_source_proposals(runtime.proposal_store),
            request_id=request_id,
        ),
        resource_type="source_proposal_registry",
    )


@mcp_server.tool()
def get_source_proposal(
    proposal_id: str,
    ctx: Context,
) -> SourceProposalDetailsToolResult:
    """Get safe review details for one durable pending source proposal."""

    return _execute_tool(
        ctx=ctx,
        action="get_source_proposal",
        operation=lambda runtime, request_id: SourceProposalDetailsToolResult(
            proposal=registered_source_proposal(
                runtime.proposal_store,
                proposal_id,
            ),
            request_id=request_id,
        ),
        resource_id=proposal_id,
        resource_type="source_proposal",
        proposal_id=lambda result: result.proposal.proposal_id,
    )


@mcp_server.tool()
def resolve_source_artifact(
    source_id: str,
    ctx: Context,
    name: str | None = None,
    logical_path: str | None = None,
    artifact_type: str | None = None,
) -> ArtifactReferenceToolResult:
    """Resolve an exact source-scoped selector to an opaque actionable handle."""

    return _execute_tool(
        ctx=ctx,
        action="resolve_source_artifact",
        operation=lambda runtime, request_id: ArtifactReferenceToolResult(
            **resolve_authorized_source_artifact(
                registry=runtime.registry,
                source_policy=runtime.source_policy,
                workspace_adapter=runtime.workspace_adapter,
                reference_codec=_reference_codec(runtime),
                source_id=source_id,
                name=name,
                logical_path=logical_path,
                artifact_type=artifact_type,
            ).model_dump(),
            request_id=request_id,
        ),
        source_id=source_id,
        resource_type="source_artifact_reference",
        result_resource_id=lambda result: result.artifact_ref,
    )


@mcp_server.tool()
def copy_source_artifact(
    source_id: str,
    artifact_ref: str,
    name: str,
    ctx: Context,
    approval_reference: str = "",
    destination_ref: str | None = None,
) -> ArtifactReferenceMutationToolResult:
    """Copy one native Google Doc while preserving the source artifact."""

    return _execute_tool(
        ctx=ctx,
        action="copy_source_artifact",
        operation=lambda runtime, request_id: ArtifactReferenceMutationToolResult(
            **copy_authorized_source_artifact(
                mutation_policy=runtime.mutation_policy,
                source_policy=runtime.source_policy,
                workspace_adapter=runtime.workspace_adapter,
                reference_codec=_reference_codec(runtime),
                source_id=source_id,
                artifact_ref=artifact_ref,
                name=name,
                approval_reference=approval_reference,
                destination_ref=destination_ref,
            ).model_dump(),
            request_id=request_id,
        ),
        source_id=source_id,
        resource_id=artifact_ref,
        resource_type="google_document",
        result_resource_id=lambda result: result.artifact.artifact_ref,
        approval_reference=ContentMutationPolicy.normalized_approval_reference(approval_reference),
    )


@mcp_server.tool()
def rename_source_artifact(
    source_id: str,
    artifact_ref: str,
    name: str,
    ctx: Context,
    approval_reference: str = "",
) -> ArtifactReferenceMutationToolResult:
    """Rename one source-scoped artifact through the governed update capability."""

    return _execute_tool(
        ctx=ctx,
        action="rename_source_artifact",
        operation=lambda runtime, request_id: ArtifactReferenceMutationToolResult(
            **rename_authorized_source_artifact(
                mutation_policy=runtime.mutation_policy,
                source_policy=runtime.source_policy,
                workspace_adapter=runtime.workspace_adapter,
                reference_codec=_reference_codec(runtime),
                source_id=source_id,
                artifact_ref=artifact_ref,
                name=name,
                approval_reference=approval_reference,
            ).model_dump(),
            request_id=request_id,
        ),
        source_id=source_id,
        resource_id=artifact_ref,
        resource_type="google_drive_artifact",
        result_resource_id=lambda result: result.artifact.artifact_ref,
        approval_reference=ContentMutationPolicy.normalized_approval_reference(approval_reference),
    )


@mcp_server.tool()
def inspect_document_structure(
    source_id: str,
    artifact_ref: str,
    ctx: Context,
) -> DocumentStructureToolResult:
    """Inspect bounded Google Docs topology, indexes, styles, and placeholders."""

    return _execute_tool(
        ctx=ctx,
        action="inspect_document_structure",
        operation=lambda runtime, request_id: DocumentStructureToolResult(
            **inspect_authorized_document_structure(
                registry=runtime.registry,
                source_policy=runtime.source_policy,
                workspace_adapter=runtime.workspace_adapter,
                docs_adapter=runtime.docs_adapter,
                reference_codec=_reference_codec(runtime),
                source_id=source_id,
                artifact_ref=artifact_ref,
            ).model_dump(),
            request_id=request_id,
        ),
        source_id=source_id,
        resource_id=artifact_ref,
        resource_type="google_document_structure",
    )


@mcp_server.tool()
def inspect_document_tab(
    source_id: str,
    artifact_ref: str,
    tab_ref: str,
    ctx: Context,
) -> DocumentTabInspectionToolResult:
    """Inspect one document tab selected by an opaque artifact-bound reference."""

    return _execute_tool(
        ctx=ctx,
        action="inspect_document_tab",
        operation=lambda runtime, request_id: DocumentTabInspectionToolResult(
            **inspect_authorized_document_tab(
                registry=runtime.registry,
                source_policy=runtime.source_policy,
                workspace_adapter=runtime.workspace_adapter,
                docs_adapter=runtime.docs_adapter,
                reference_codec=_reference_codec(runtime),
                source_id=source_id,
                artifact_ref=artifact_ref,
                tab_ref=tab_ref,
            ).model_dump(),
            request_id=request_id,
        ),
        source_id=source_id,
        resource_id=artifact_ref,
        resource_type="google_document_tab",
    )


@mcp_server.tool()
def create_document_tab(
    source_id: str,
    artifact_ref: str,
    title: str,
    required_revision_id: str,
    ctx: Context,
    index: int | None = None,
    parent_tab_ref: str | None = None,
    approval_reference: str = "",
) -> DocumentTabMutationToolResult:
    """Create one governed document tab at an inspected revision."""

    return _execute_tool(
        ctx=ctx,
        action="create_document_tab",
        operation=lambda runtime, request_id: DocumentTabMutationToolResult(
            **create_authorized_document_tab(
                mutation_policy=runtime.mutation_policy,
                source_policy=runtime.source_policy,
                workspace_adapter=runtime.workspace_adapter,
                docs_adapter=runtime.docs_adapter,
                reference_codec=_reference_codec(runtime),
                source_id=source_id,
                artifact_ref=artifact_ref,
                title=title,
                required_revision_id=required_revision_id,
                approval_reference=approval_reference,
                index=index,
                parent_tab_ref=parent_tab_ref,
            ).model_dump(),
            request_id=request_id,
        ),
        source_id=source_id,
        resource_id=artifact_ref,
        resource_type="google_document_tab",
        approval_reference=ContentMutationPolicy.normalized_approval_reference(
            approval_reference
        ),
    )


@mcp_server.tool()
def rename_document_tab(
    source_id: str,
    artifact_ref: str,
    tab_ref: str,
    title: str,
    required_revision_id: str,
    ctx: Context,
    approval_reference: str = "",
) -> DocumentTabMutationToolResult:
    """Rename one governed document tab selected by an opaque reference."""

    return _execute_tool(
        ctx=ctx,
        action="rename_document_tab",
        operation=lambda runtime, request_id: DocumentTabMutationToolResult(
            **rename_authorized_document_tab(
                mutation_policy=runtime.mutation_policy,
                source_policy=runtime.source_policy,
                workspace_adapter=runtime.workspace_adapter,
                docs_adapter=runtime.docs_adapter,
                reference_codec=_reference_codec(runtime),
                source_id=source_id,
                artifact_ref=artifact_ref,
                tab_ref=tab_ref,
                title=title,
                required_revision_id=required_revision_id,
                approval_reference=approval_reference,
            ).model_dump(),
            request_id=request_id,
        ),
        source_id=source_id,
        resource_id=artifact_ref,
        resource_type="google_document_tab",
        approval_reference=ContentMutationPolicy.normalized_approval_reference(
            approval_reference
        ),
    )


@mcp_server.tool()
def delete_document_tab(
    source_id: str,
    artifact_ref: str,
    tab_ref: str,
    required_revision_id: str,
    ctx: Context,
    approval_reference: str = "",
) -> DocumentTabMutationToolResult:
    """Delete one leaf document tab while preserving at least one tab."""

    return _execute_tool(
        ctx=ctx,
        action="delete_document_tab",
        operation=lambda runtime, request_id: DocumentTabMutationToolResult(
            **delete_authorized_document_tab(
                mutation_policy=runtime.mutation_policy,
                source_policy=runtime.source_policy,
                workspace_adapter=runtime.workspace_adapter,
                docs_adapter=runtime.docs_adapter,
                reference_codec=_reference_codec(runtime),
                source_id=source_id,
                artifact_ref=artifact_ref,
                tab_ref=tab_ref,
                required_revision_id=required_revision_id,
                approval_reference=approval_reference,
            ).model_dump(),
            request_id=request_id,
        ),
        source_id=source_id,
        resource_id=artifact_ref,
        resource_type="google_document_tab",
        approval_reference=ContentMutationPolicy.normalized_approval_reference(
            approval_reference
        ),
    )


@mcp_server.tool()
def edit_source_document(
    source_id: str,
    artifact_ref: str,
    required_revision_id: str,
    operations: list[DocumentEditOperation],
    ctx: Context,
    tab_ref: str | None = None,
    approval_reference: str = "",
) -> DocumentEditToolResult:
    """Apply only allowlisted semantic Docs operations at an inspected revision."""

    return _execute_tool(
        ctx=ctx,
        action="edit_source_document",
        operation=lambda runtime, request_id: DocumentEditToolResult(
            **edit_authorized_source_document(
                mutation_policy=runtime.mutation_policy,
                source_policy=runtime.source_policy,
                workspace_adapter=runtime.workspace_adapter,
                docs_adapter=runtime.docs_adapter,
                reference_codec=_reference_codec(runtime),
                source_id=source_id,
                artifact_ref=artifact_ref,
                required_revision_id=required_revision_id,
                operations=operations,
                tab_ref=tab_ref,
                approval_reference=approval_reference,
            ).model_dump(),
            request_id=request_id,
        ),
        source_id=source_id,
        resource_id=artifact_ref,
        resource_type="google_document",
        approval_reference=ContentMutationPolicy.normalized_approval_reference(approval_reference),
    )


@mcp_server.tool()
def validate_document_structure(
    source_id: str,
    artifact_ref: str,
    requirements: DocumentQualityRequirements,
    ctx: Context,
    expected_revision_id: str | None = None,
) -> DocumentQualityToolResult:
    """Run a read-only structural quality gate against one inspected document."""

    return _execute_tool(
        ctx=ctx,
        action="validate_document_structure",
        operation=lambda runtime, request_id: DocumentQualityToolResult(
            **validate_authorized_document_structure(
                registry=runtime.registry,
                source_policy=runtime.source_policy,
                workspace_adapter=runtime.workspace_adapter,
                docs_adapter=runtime.docs_adapter,
                reference_codec=_reference_codec(runtime),
                source_id=source_id,
                artifact_ref=artifact_ref,
                requirements=requirements,
                expected_revision_id=expected_revision_id,
            ).model_dump(),
            request_id=request_id,
        ),
        source_id=source_id,
        resource_id=artifact_ref,
        resource_type="google_document_quality",
    )


@mcp_server.tool()
def create_source_artifact(
    source_id: str,
    name: str,
    type: Literal["document"],
    ctx: Context,
    approval_reference: str = "",
) -> SourceMutationToolResult:
    """Create one native Google Doc at an approved, create-enabled source root."""

    return _execute_tool(
        ctx=ctx,
        action="create_source_artifact",
        operation=lambda runtime, request_id: SourceMutationToolResult(
            **create_authorized_source_artifact(
                mutation_policy=runtime.mutation_policy,
                workspace_adapter=runtime.workspace_adapter,
                source_id=source_id,
                name=name,
                artifact_type=type,
                approval_reference=approval_reference,
            ).model_dump(exclude={"request_id"}),
            request_id=request_id,
        ),
        source_id=source_id,
        resource_type="google_document",
        result_resource_id=lambda result: result.artifact.id,
        approval_reference=ContentMutationPolicy.normalized_approval_reference(
            approval_reference
        ),
    )


@mcp_server.tool()
def update_source_artifact(
    source_id: str,
    change: str,
    ctx: Context,
    artifact_ref: str | None = None,
    document_id: str | None = None,
    approval_reference: str = "",
) -> SourceMutationToolResult:
    """Append bounded text to an authorized native Google Doc."""

    return _execute_tool(
        ctx=ctx,
        action="update_source_artifact",
        operation=lambda runtime, request_id: SourceMutationToolResult(
            **update_authorized_source_artifact(
                mutation_policy=runtime.mutation_policy,
                source_policy=runtime.source_policy,
                workspace_adapter=runtime.workspace_adapter,
                docs_adapter=runtime.docs_adapter,
                source_id=source_id,
                document_id=_resolved_artifact_id(
                    runtime,
                    source_id=source_id,
                    artifact_ref=artifact_ref,
                    legacy_id=document_id,
                ),
                change=change,
                approval_reference=approval_reference,
            ).model_dump(exclude={"request_id"}),
            request_id=request_id,
        ),
        source_id=source_id,
        resource_id=artifact_ref or document_id,
        resource_type="google_document",
        approval_reference=ContentMutationPolicy.normalized_approval_reference(
            approval_reference
        ),
    )


@mcp_server.tool()
def move_source_artifact(
    source_id: str,
    ctx: Context,
    artifact_ref: str | None = None,
    artifact_id: str | None = None,
    destination_folder_id: str | None = None,
    approval_reference: str = "",
    destination_source_id: str | None = None,
) -> SourceMutationToolResult:
    """Move an artifact within its source or to an approved archive destination."""

    return _execute_tool(
        ctx=ctx,
        action="move_source_artifact",
        operation=lambda runtime, request_id: SourceMutationToolResult(
            **move_authorized_source_artifact(
                mutation_policy=runtime.mutation_policy,
                source_policy=runtime.source_policy,
                workspace_adapter=runtime.workspace_adapter,
                source_id=source_id,
                artifact_id=_resolved_artifact_id(
                    runtime,
                    source_id=source_id,
                    artifact_ref=artifact_ref,
                    legacy_id=artifact_id,
                ),
                destination_folder_id=destination_folder_id,
                destination_source_id=destination_source_id,
                approval_reference=approval_reference,
            ).model_dump(exclude={"request_id"}),
            request_id=request_id,
        ),
        source_id=source_id,
        resource_id=artifact_ref or artifact_id,
        resource_type="google_drive_artifact",
        approval_reference=ContentMutationPolicy.normalized_approval_reference(
            approval_reference
        ),
        destination_source_id=destination_source_id,
    )


@mcp_server.tool()
def convert_source_artifact(
    source_id: str,
    target_type: ArtifactConversionTarget,
    ctx: Context,
    artifact_ref: str | None = None,
    artifact_id: str | None = None,
    approval_reference: str = "",
) -> ArtifactConversionToolResult:
    """Convert Office to Google native; opaque references are the preferred input."""

    def operation(runtime: KnowledgeRuntime, request_id: str) -> ArtifactConversionToolResult:
        codec = _reference_codec(runtime)
        if artifact_ref:
            resolved_id = codec.decode(artifact_ref, source_id=source_id)
        elif artifact_id:
            # Compatibility path for pre-v0.18 consumers. New discovery flows do not
            # expose Drive IDs and should always send artifact_ref.
            resolved_id = artifact_id
        else:
            raise WorkspaceAdapterError(
                "artifact_reference_required",
                "An artifact reference is required.",
                422,
            )
        converted = convert_authorized_source_artifact(
            mutation_policy=runtime.mutation_policy,
            source_policy=runtime.source_policy,
            workspace_adapter=runtime.workspace_adapter,
            source_id=source_id,
            artifact_id=resolved_id,
            target_type=target_type,
            approval_reference=approval_reference,
        )
        original_ref = artifact_ref or codec.encode(
            source_id=source_id, artifact_id=converted.original_artifact.id
        )
        return ArtifactConversionToolResult(
            original_artifact=ArtifactReference(
                artifact_ref=original_ref,
                name=converted.original_artifact.name,
                type=converted.original_artifact.type,
                source_id=source_id,
            ),
            created_artifact=ArtifactReference(
                artifact_ref=codec.encode(
                    source_id=source_id, artifact_id=converted.created_artifact.id
                ),
                name=converted.created_artifact.name,
                type=converted.created_artifact.type,
                source_id=source_id,
            ),
            created_artifact_type=converted.created_artifact_type.value,
            source_id=source_id,
            request_id=request_id,
        )

    return _execute_tool(
        ctx=ctx,
        action="convert_source_artifact",
        operation=operation,
        source_id=source_id,
        resource_id=artifact_ref,
        resource_type="office_artifact",
        created_resource_id=lambda result: result.created_artifact.artifact_ref,
        approval_reference=ContentMutationPolicy.normalized_approval_reference(
            approval_reference
        ),
    )


@mcp_server.tool()
def delete_source_artifact(
    source_id: str,
    ctx: Context,
    artifact_ref: str | None = None,
    artifact_id: str | None = None,
    approval_reference: str = "",
) -> SourceMutationToolResult:
    """Move an authorized source artifact to Drive trash."""

    return _execute_tool(
        ctx=ctx,
        action="delete_source_artifact",
        operation=lambda runtime, request_id: SourceMutationToolResult(
            **delete_authorized_source_artifact(
                mutation_policy=runtime.mutation_policy,
                source_policy=runtime.source_policy,
                workspace_adapter=runtime.workspace_adapter,
                source_id=source_id,
                artifact_id=_resolved_artifact_id(
                    runtime,
                    source_id=source_id,
                    artifact_ref=artifact_ref,
                    legacy_id=artifact_id,
                ),
                approval_reference=approval_reference,
            ).model_dump(exclude={"request_id"}),
            request_id=request_id,
        ),
        source_id=source_id,
        resource_id=artifact_ref or artifact_id,
        resource_type="google_drive_artifact",
        approval_reference=ContentMutationPolicy.normalized_approval_reference(
            approval_reference
        ),
    )


@mcp_server.tool()
def share_source_artifact(
    source_id: str,
    audience: str,
    ctx: Context,
    artifact_ref: str | None = None,
    artifact_id: str | None = None,
    approval_reference: str = "",
) -> SourceMutationToolResult:
    """Grant reader access to one explicit audience for an authorized artifact."""

    return _execute_tool(
        ctx=ctx,
        action="share_source_artifact",
        operation=lambda runtime, request_id: SourceMutationToolResult(
            **share_authorized_source_artifact(
                mutation_policy=runtime.mutation_policy,
                source_policy=runtime.source_policy,
                workspace_adapter=runtime.workspace_adapter,
                source_id=source_id,
                artifact_id=_resolved_artifact_id(
                    runtime,
                    source_id=source_id,
                    artifact_ref=artifact_ref,
                    legacy_id=artifact_id,
                ),
                audience=audience,
                approval_reference=approval_reference,
            ).model_dump(exclude={"request_id"}),
            request_id=request_id,
        ),
        source_id=source_id,
        resource_id=artifact_ref or artifact_id,
        resource_type="google_drive_artifact",
        approval_reference=ContentMutationPolicy.normalized_approval_reference(
            approval_reference
        ),
        audience=ContentMutationPolicy.normalized_audience(audience),
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
def inspect_source_artifacts(
    source_id: str,
    ctx: Context,
) -> ArtifactInspectionToolResult:
    """Inspect safe native and Office artifact metadata in one approved source."""

    def operation(
        runtime: KnowledgeRuntime,
        request_id: str,
    ) -> ArtifactInspectionToolResult:
        _, artifacts = inspect_authorized_source_artifacts(
            registry=runtime.registry,
            source_policy=runtime.source_policy,
            workspace_adapter=runtime.workspace_adapter,
            source_id=source_id,
        )
        return ArtifactInspectionToolResult(
            artifacts=artifacts,
            request_id=request_id,
        )

    return _execute_tool(
        ctx=ctx,
        action="inspect_source_artifacts",
        operation=operation,
        source_id=source_id,
        resource_type="source_artifact_metadata",
    )


@mcp_server.tool()
def retrieve_document(
    source_id: str,
    ctx: Context,
    artifact_ref: str | None = None,
    document_id: str | None = None,
) -> GoogleDocContent:
    """Retrieve an authorized document that belongs to the selected source."""

    def operation(runtime: KnowledgeRuntime, request_id: str) -> GoogleDocContent:
        _, document = retrieve_authorized_document(
            registry=runtime.registry,
            source_policy=runtime.source_policy,
            workspace_adapter=runtime.workspace_adapter,
            docs_adapter=runtime.docs_adapter,
            source_id=source_id,
            document_id=_resolved_artifact_id(
                runtime,
                source_id=source_id,
                artifact_ref=artifact_ref,
                legacy_id=document_id,
            ),
        )
        return document.model_copy(update={"request_id": request_id})

    return _execute_tool(
        ctx=ctx,
        action="retrieve_document",
        operation=operation,
        source_id=source_id,
        resource_id=artifact_ref or document_id,
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


async def _execute_hubspot_tool(
    *,
    ctx: Context,
    tool_name: str,
    arguments: dict[str, Any],
    approval_reference: str | None = None,
    explicit_intent: bool = False,
) -> HubSpotToolResult:
    request_id = _mcp_request_id(ctx)
    runtime = get_hubspot_runtime()
    decision = runtime.policy.classify(tool_name)
    try:
        result = await runtime.mcp_client.call_tool(
            tool_name,
            arguments,
            approval_reference=approval_reference,
            explicit_intent=explicit_intent,
        )
        if tool_name == "get_user_details":
            runtime.token_store.update_account(account_metadata_from_result(result))
        emit_audit_record(
            request_id=request_id,
            action="hubspot_tool_call",
            resource_id=None,
            resource_type="hubspot_mcp_tool",
            result="success",
            http_status=200,
            provider="hubspot",
            tool=tool_name,
            operation_classification=decision.classification,
            approval_reference=approval_reference,
        )
        return HubSpotToolResult(tool=tool_name, result=result, request_id=request_id)
    except WorkspaceAdapterError as error:
        emit_audit_record(
            request_id=request_id,
            action="hubspot_tool_call",
            resource_id=None,
            resource_type="hubspot_mcp_tool",
            result="rejected" if error.status_code < 500 else "error",
            http_status=error.status_code,
            error_code=error.code,
            provider="hubspot",
            tool=tool_name,
            operation_classification=decision.classification,
            approval_reference=approval_reference,
        )
        raise RuntimeError(f"{error.code}: {error.message}") from error
    except Exception:
        emit_audit_record(
            request_id=request_id,
            action="hubspot_tool_call",
            resource_id=None,
            resource_type="hubspot_mcp_tool",
            result="error",
            http_status=500,
            error_code="unhandled_error",
            provider="hubspot",
            tool=tool_name,
            operation_classification=decision.classification,
            approval_reference=approval_reference,
        )
        raise


@mcp_server.tool()
async def hubspot_list_tools(ctx: Context) -> HubSpotToolListResult:
    """List live HubSpot tools with Brunova's safe governance classification."""

    request_id = _mcp_request_id(ctx)
    try:
        tools = await get_hubspot_runtime().mcp_client.list_tools()
        emit_audit_record(
            request_id=request_id,
            action="hubspot_list_tools",
            resource_id=None,
            resource_type="hubspot_mcp_tool_catalog",
            result="success",
            http_status=200,
            provider="hubspot",
            operation_classification="read",
        )
        return HubSpotToolListResult(tools=tools, request_id=request_id)
    except WorkspaceAdapterError as error:
        emit_audit_record(
            request_id=request_id,
            action="hubspot_list_tools",
            resource_id=None,
            resource_type="hubspot_mcp_tool_catalog",
            result="error",
            http_status=error.status_code,
            error_code=error.code,
            provider="hubspot",
            operation_classification="read",
        )
        raise RuntimeError(f"{error.code}: {error.message}") from error


@mcp_server.tool()
async def n8n_status(ctx: Context) -> N8NStatusResult:
    """Return safe n8n MCP connectivity and live catalog status."""

    request_id = _mcp_request_id(ctx)
    started = time.monotonic()
    try:
        status = await get_n8n_client().status()
    except ValueError:
        result = N8NStatusResult(
            configured=False, connected=False, mcp_initialized=False,
            tool_count=0, request_id=request_id,
        )
    else:
        result = N8NStatusResult(**status.model_dump(), request_id=request_id)
    emit_audit_record(
        request_id=request_id, action="n8n_status", resource_id=None,
        resource_type="n8n_mcp_status",
        result="success" if result.connected else "error",
        http_status=200 if result.connected else 503, provider="n8n",
        duration_ms=round((time.monotonic() - started) * 1000),
    )
    return result


@mcp_server.tool()
async def n8n_list_tools(ctx: Context) -> N8NToolListResult:
    """List the current unfiltered n8n MCP catalog from live discovery."""

    request_id = _mcp_request_id(ctx)
    started = time.monotonic()
    try:
        tools = await get_n8n_client().list_tools(force_refresh=True)
        emit_audit_record(
            request_id=request_id, action="n8n_list_tools", resource_id=None,
            resource_type="n8n_mcp_tool_catalog", result="success", http_status=200,
            provider="n8n", duration_ms=round((time.monotonic() - started) * 1000),
        )
        return N8NToolListResult(tools=tools, request_id=request_id)
    except (ValueError, WorkspaceAdapterError) as error:
        code = error.code if isinstance(error, WorkspaceAdapterError) else "n8n_not_configured"
        status = error.status_code if isinstance(error, WorkspaceAdapterError) else 503
        emit_audit_record(
            request_id=request_id, action="n8n_list_tools", resource_id=None,
            resource_type="n8n_mcp_tool_catalog", result="error", http_status=status,
            error_code=code, provider="n8n",
            duration_ms=round((time.monotonic() - started) * 1000),
        )
        raise RuntimeError(f"{code}: n8n MCP catalog is unavailable") from error


@mcp_server.tool()
async def hubspot_get_user_details(ctx: Context) -> HubSpotToolResult:
    """Return HubSpot account, user, permission, and capability details."""

    return await _execute_hubspot_tool(ctx=ctx, tool_name="get_user_details", arguments={})


@mcp_server.tool()
async def hubspot_search_crm_objects(
    arguments: dict[str, Any], ctx: Context
) -> HubSpotToolResult:
    """Search HubSpot CRM objects using the official downstream tool arguments."""

    return await _execute_hubspot_tool(
        ctx=ctx, tool_name="search_crm_objects", arguments=arguments
    )


@mcp_server.tool()
async def hubspot_get_crm_objects(
    arguments: dict[str, Any], ctx: Context
) -> HubSpotToolResult:
    """Fetch HubSpot CRM objects using the official downstream tool arguments."""

    return await _execute_hubspot_tool(
        ctx=ctx, tool_name="get_crm_objects", arguments=arguments
    )


@mcp_server.tool()
async def hubspot_search_properties(
    arguments: dict[str, Any], ctx: Context
) -> HubSpotToolResult:
    """Search HubSpot CRM property definitions."""

    return await _execute_hubspot_tool(
        ctx=ctx, tool_name="search_properties", arguments=arguments
    )


@mcp_server.tool()
async def hubspot_get_properties(
    arguments: dict[str, Any], ctx: Context
) -> HubSpotToolResult:
    """Fetch full HubSpot property definitions."""

    return await _execute_hubspot_tool(
        ctx=ctx, tool_name="get_properties", arguments=arguments
    )


@mcp_server.tool()
async def hubspot_search_owners(
    arguments: dict[str, Any], ctx: Context
) -> HubSpotToolResult:
    """Search HubSpot CRM record owners."""

    return await _execute_hubspot_tool(
        ctx=ctx, tool_name="search_owners", arguments=arguments
    )


@mcp_server.tool()
async def hubspot_call_read_tool(
    tool_name: str,
    arguments: dict[str, Any],
    ctx: Context,
) -> HubSpotToolResult:
    """Call a live HubSpot tool only when it is explicitly classified read-only."""

    return await _execute_hubspot_tool(
        ctx=ctx,
        tool_name=tool_name,
        arguments=arguments,
    )


@mcp_server.tool()
async def hubspot_manage_crm_objects(
    arguments: dict[str, Any],
    approval_reference: str,
    explicit_intent: bool,
    ctx: Context,
) -> HubSpotToolResult:
    """Create or update HubSpot CRM data after explicit human approval."""

    return await _execute_hubspot_tool(
        ctx=ctx,
        tool_name="manage_crm_objects",
        arguments=arguments,
        approval_reference=approval_reference,
        explicit_intent=explicit_intent,
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
