"""Semantic Knowledge Gateway operations shared by HTTP and MCP interfaces."""

from app.adapters.google_workspace.docs import GoogleDocsAdapter
from app.adapters.google_workspace.drive import GoogleWorkspaceAdapter
from app.adapters.google_workspace.errors import WorkspaceAdapterError
from app.adapters.google_workspace.models import (
    DriveFile,
    GoogleDocContent,
    SheetRangeContent,
    SourceMetadata,
)
from app.adapters.google_workspace.sheets import GoogleSheetsAdapter
from app.policies.source_access import SourceAccessPolicy
from app.policies.workspace import ContentReadPolicy, DriveReadPolicy
from app.source_discovery.interface import (
    CandidateDetailsResponse,
    DiscoveryResult,
    SourceDiscovery,
)
from app.source_governance import (
    SourceProposalDetails,
    SourceProposalRecord,
    SourceProposalSummary,
    candidate_details,
    create_source_proposal as build_source_proposal,
)
from app.source_proposal_store import SourceProposalStore
from app.source_registry import (
    Classification,
    SourceDefinition,
    SourceRegistry,
    SourceRegistryMetadata,
)

SOURCE_CANDIDATE_LOOKUP_LIMIT = 100


def registered_source(registry: SourceRegistry, source_id: str) -> SourceDefinition:
    try:
        return registry.get(source_id)
    except KeyError as error:
        raise WorkspaceAdapterError(
            "source_not_found",
            "The requested knowledge source is not registered.",
            404,
        ) from error


def list_registered_sources(registry: SourceRegistry) -> list[SourceRegistryMetadata]:
    return [
        SourceRegistryMetadata.from_definition(source) for source in registry.sources
    ]


def discover_candidate_sources(
    *,
    source_discovery: SourceDiscovery,
    limit: int,
) -> DiscoveryResult:
    """Discover source roots only; never register or mutate a source."""

    safe_limit = DriveReadPolicy.validate_list_limit(limit)
    return source_discovery.discover(limit=safe_limit)


def get_candidate_details(
    *,
    source_discovery: SourceDiscovery,
    candidate_id: str,
) -> CandidateDetailsResponse:
    result = discover_candidate_sources(
        source_discovery=source_discovery,
        limit=SOURCE_CANDIDATE_LOOKUP_LIMIT,
    )
    return candidate_details(result, candidate_id)


def register_source_proposal(
    *,
    source_discovery: SourceDiscovery,
    proposal_store: SourceProposalStore,
    candidate_id: str,
    name: str,
    classification: Classification,
    reason: str,
    request_id: str,
) -> SourceProposalRecord:
    """Create an auditable pending intent without touching Source Registry."""

    result = discover_candidate_sources(
        source_discovery=source_discovery,
        limit=SOURCE_CANDIDATE_LOOKUP_LIMIT,
    )
    proposal = build_source_proposal(
        result=result,
        candidate_id=candidate_id,
        name=name,
        classification=classification,
        reason=reason,
        request_id=request_id,
    )
    return proposal_store.create(proposal)


def list_registered_source_proposals(
    proposal_store: SourceProposalStore,
) -> list[SourceProposalSummary]:
    return [
        SourceProposalSummary.from_record(proposal)
        for proposal in proposal_store.list()
    ]


def registered_source_proposal(
    proposal_store: SourceProposalStore,
    proposal_id: str,
) -> SourceProposalDetails:
    return SourceProposalDetails.from_record(proposal_store.get(proposal_id))


def list_authorized_source_files(
    *,
    registry: SourceRegistry,
    source_policy: SourceAccessPolicy,
    workspace_adapter: GoogleWorkspaceAdapter,
    source_id: str,
    limit: int,
) -> tuple[SourceDefinition, list[DriveFile]]:
    source = registered_source(registry, source_id)
    allowed_source = source_policy.authorize_source(source)
    safe_limit = DriveReadPolicy.validate_list_limit(limit)
    files = workspace_adapter.list_source_files(
        source=allowed_source,
        limit=safe_limit,
        source_policy=source_policy,
    )
    return source, files


def retrieve_authorized_document(
    *,
    registry: SourceRegistry,
    source_policy: SourceAccessPolicy,
    workspace_adapter: GoogleWorkspaceAdapter,
    docs_adapter: GoogleDocsAdapter,
    source_id: str,
    document_id: str,
) -> tuple[SourceDefinition, GoogleDocContent]:
    source = registered_source(registry, source_id)
    allowed_source = source_policy.authorize_source(source)
    safe_id = ContentReadPolicy.validate_resource_id(document_id)
    ContentReadPolicy.validate_document_limit(docs_adapter.max_chars)
    resource = workspace_adapter.get_resource(safe_id)
    context = source_policy.authorize_resource_for_source(resource, allowed_source)
    result = docs_adapter.get_document(resource, max_chars=docs_adapter.max_chars)
    return source, result.model_copy(
        update={
            "source": SourceMetadata(
                id=context.source_id,
                name=context.source_name,
                classification=context.classification,
            )
        }
    )


def retrieve_authorized_sheet_range(
    *,
    registry: SourceRegistry,
    source_policy: SourceAccessPolicy,
    workspace_adapter: GoogleWorkspaceAdapter,
    sheets_adapter: GoogleSheetsAdapter,
    source_id: str,
    spreadsheet_id: str,
    range_name: str,
) -> tuple[SourceDefinition, SheetRangeContent]:
    source = registered_source(registry, source_id)
    allowed_source = source_policy.authorize_source(source)
    safe_id = ContentReadPolicy.validate_resource_id(spreadsheet_id)
    safe_range = ContentReadPolicy.validate_sheet_range(
        range_name,
        max_cells=sheets_adapter.max_cells,
    )
    resource = workspace_adapter.get_resource(safe_id)
    context = source_policy.authorize_resource_for_source(resource, allowed_source)
    result = sheets_adapter.get_range(resource, range_name=safe_range)
    return source, result.model_copy(
        update={
            "source": SourceMetadata(
                id=context.source_id,
                name=context.source_name,
                classification=context.classification,
            )
        }
    )
