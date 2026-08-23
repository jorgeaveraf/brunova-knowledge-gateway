"""Semantic Knowledge Gateway operations shared by HTTP and MCP interfaces."""

from app.adapters.google_workspace.docs import GoogleDocsAdapter
from app.adapters.google_workspace.drive import GoogleWorkspaceAdapter
from app.adapters.google_workspace.errors import WorkspaceAdapterError
from app.adapters.google_workspace.models import (
    ArtifactConversionResult,
    ArtifactConversionTarget,
    ArtifactMetadata,
    DriveFile,
    GoogleDocContent,
    SheetRangeContent,
    SourceArtifact,
    SourceArtifactMutationResult,
    SourceMetadata,
)
from app.adapters.google_workspace.sheets import GoogleSheetsAdapter
from app.policies.content_mutation import ContentMutationPolicy, MutationOperation
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
GOOGLE_DOC_MIME_TYPE = "application/vnd.google-apps.document"
ARTIFACT_TYPES = {
    "application/vnd.google-apps.document": "document",
    "application/vnd.google-apps.spreadsheet": "spreadsheet",
    "application/vnd.google-apps.presentation": "presentation",
    "application/vnd.google-apps.folder": "folder",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.ms-excel.sheet.macroenabled.12": "xlsm",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
}
CONVERSION_TARGET_MIME_TYPES = {
    ArtifactConversionTarget.GOOGLE_DOCUMENT: "application/vnd.google-apps.document",
    ArtifactConversionTarget.GOOGLE_SHEET: "application/vnd.google-apps.spreadsheet",
    ArtifactConversionTarget.GOOGLE_PRESENTATION: (
        "application/vnd.google-apps.presentation"
    ),
}
OFFICE_CONVERSION_TARGETS = {
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": (
        ArtifactConversionTarget.GOOGLE_SHEET
    ),
    "application/vnd.ms-excel.sheet.macroenabled.12": (
        ArtifactConversionTarget.GOOGLE_SHEET
    ),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": (
        ArtifactConversionTarget.GOOGLE_DOCUMENT
    ),
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": (
        ArtifactConversionTarget.GOOGLE_PRESENTATION
    ),
}


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


def create_authorized_source_artifact(
    *,
    mutation_policy: ContentMutationPolicy,
    workspace_adapter: GoogleWorkspaceAdapter,
    source_id: str,
    name: str,
    artifact_type: str,
    approval_reference: str,
) -> SourceArtifactMutationResult:
    if artifact_type != "document":
        raise WorkspaceAdapterError(
            "mutation_type_not_supported",
            "Only native Google Docs can be created by this Gateway.",
            422,
        )
    allowed_source = mutation_policy.authorize(
        source_id=source_id,
        operation=MutationOperation.CREATE,
        approval_reference=approval_reference,
    )
    safe_name = mutation_policy.validate_name(name)
    resource = workspace_adapter.create_document(
        source=allowed_source,
        name=safe_name,
    )
    return _mutation_result(resource, allowed_source, status="created")


def update_authorized_source_artifact(
    *,
    mutation_policy: ContentMutationPolicy,
    source_policy: SourceAccessPolicy,
    workspace_adapter: GoogleWorkspaceAdapter,
    docs_adapter: GoogleDocsAdapter,
    source_id: str,
    document_id: str,
    change: str,
    approval_reference: str,
) -> SourceArtifactMutationResult:
    allowed_source = mutation_policy.authorize(
        source_id=source_id,
        operation=MutationOperation.UPDATE,
        approval_reference=approval_reference,
    )
    safe_id = mutation_policy.validate_resource_id(document_id)
    safe_change = mutation_policy.validate_change(change)
    resource = workspace_adapter.get_resource(safe_id)
    source_policy.authorize_resource_for_source(resource, allowed_source)
    if resource.mime_type != GOOGLE_DOC_MIME_TYPE:
        raise WorkspaceAdapterError(
            "resource_type_invalid",
            "Only native Google Docs can be updated by this Gateway.",
            422,
        )
    docs_adapter.append_text(resource, text=safe_change)
    return _mutation_result(resource, allowed_source, status="updated")


def move_authorized_source_artifact(
    *,
    mutation_policy: ContentMutationPolicy,
    source_policy: SourceAccessPolicy,
    workspace_adapter: GoogleWorkspaceAdapter,
    source_id: str,
    artifact_id: str,
    destination_folder_id: str | None,
    destination_source_id: str | None,
    approval_reference: str,
) -> SourceArtifactMutationResult:
    allowed_source = mutation_policy.authorize(
        source_id=source_id,
        operation=MutationOperation.MOVE,
        approval_reference=approval_reference,
    )
    safe_artifact_id = mutation_policy.validate_resource_id(artifact_id)
    resource = workspace_adapter.get_resource(safe_artifact_id)
    source_policy.authorize_resource_for_source(resource, allowed_source)
    if destination_source_id:
        if destination_folder_id:
            raise WorkspaceAdapterError(
                "mutation_destination_invalid",
                "Specify either a source folder or an archive destination, not both.",
                422,
            )
        archive_source = mutation_policy.authorize_archive_destination(
            destination_source_id
        )
        destination = workspace_adapter.get_resource(
            archive_source.definition.location_id
        )
    elif destination_folder_id:
        safe_destination_id = mutation_policy.validate_resource_id(
            destination_folder_id
        )
        destination = workspace_adapter.get_resource(safe_destination_id)
        source_policy.authorize_resource_for_source(destination, allowed_source)
    else:
        raise WorkspaceAdapterError(
            "mutation_destination_invalid",
            "A destination folder or approved archive destination is required.",
            422,
        )
    moved = workspace_adapter.move_resource(
        resource=resource,
        destination=destination,
    )
    return _mutation_result(moved, allowed_source, status="moved")


def convert_authorized_source_artifact(
    *,
    mutation_policy: ContentMutationPolicy,
    source_policy: SourceAccessPolicy,
    workspace_adapter: GoogleWorkspaceAdapter,
    source_id: str,
    artifact_id: str,
    target_type: ArtifactConversionTarget,
    approval_reference: str,
) -> ArtifactConversionResult:
    allowed_source = mutation_policy.authorize(
        source_id=source_id,
        operation=MutationOperation.CONVERT,
        approval_reference=approval_reference,
    )
    safe_artifact_id = mutation_policy.validate_resource_id(artifact_id)
    resource = workspace_adapter.get_resource(safe_artifact_id)
    source_policy.authorize_resource_for_source(resource, allowed_source)
    normalized_mime_type = resource.mime_type.casefold()
    expected_target = OFFICE_CONVERSION_TARGETS.get(normalized_mime_type)
    if expected_target is None:
        raise WorkspaceAdapterError(
            "conversion_source_type_invalid",
            "Only supported Office artifacts can be converted.",
            422,
        )
    if target_type != expected_target:
        raise WorkspaceAdapterError(
            "conversion_target_invalid",
            "The requested Google-native target is incompatible with this artifact.",
            422,
        )
    original_type = ARTIFACT_TYPES[normalized_mime_type]
    suffix = f".{original_type}"
    target_name = (
        resource.name[: -len(suffix)]
        if resource.name.casefold().endswith(suffix)
        else resource.name
    )
    created = workspace_adapter.convert_resource(
        resource=resource,
        target_mime_type=CONVERSION_TARGET_MIME_TYPES[target_type],
        target_name=target_name,
    )
    source_metadata = SourceMetadata(
        id=allowed_source.context.source_id,
        name=allowed_source.context.source_name,
        classification=allowed_source.context.classification,
    )
    return ArtifactConversionResult(
        original_artifact=SourceArtifact(
            id=resource.id,
            name=resource.name,
            type=original_type,
        ),
        created_artifact=SourceArtifact(
            id=created.id,
            name=created.name,
            type=ARTIFACT_TYPES.get(created.mime_type.casefold(), "file"),
        ),
        created_artifact_type=target_type,
        source=source_metadata,
    )


def delete_authorized_source_artifact(
    *,
    mutation_policy: ContentMutationPolicy,
    source_policy: SourceAccessPolicy,
    workspace_adapter: GoogleWorkspaceAdapter,
    source_id: str,
    artifact_id: str,
    approval_reference: str,
) -> SourceArtifactMutationResult:
    allowed_source = mutation_policy.authorize(
        source_id=source_id,
        operation=MutationOperation.DELETE,
        approval_reference=approval_reference,
    )
    safe_artifact_id = mutation_policy.validate_resource_id(artifact_id)
    _protect_source_root(safe_artifact_id, allowed_source)
    resource = workspace_adapter.get_resource(safe_artifact_id)
    source_policy.authorize_resource_for_source(resource, allowed_source)
    deleted = workspace_adapter.delete_resource(resource=resource)
    return _mutation_result(deleted, allowed_source, status="deleted")


def share_authorized_source_artifact(
    *,
    mutation_policy: ContentMutationPolicy,
    source_policy: SourceAccessPolicy,
    workspace_adapter: GoogleWorkspaceAdapter,
    source_id: str,
    artifact_id: str,
    audience: str,
    approval_reference: str,
) -> SourceArtifactMutationResult:
    allowed_source = mutation_policy.authorize(
        source_id=source_id,
        operation=MutationOperation.SHARE,
        approval_reference=approval_reference,
    )
    safe_artifact_id = mutation_policy.validate_resource_id(artifact_id)
    _protect_source_root(safe_artifact_id, allowed_source)
    safe_audience = mutation_policy.validate_audience(audience)
    resource = workspace_adapter.get_resource(safe_artifact_id)
    source_policy.authorize_resource_for_source(resource, allowed_source)
    shared = workspace_adapter.share_resource(
        resource=resource,
        audience=safe_audience,
    )
    return _mutation_result(shared, allowed_source, status="shared")


def _protect_source_root(resource_id, source) -> None:
    if resource_id == source.definition.location_id:
        raise WorkspaceAdapterError(
            "mutation_source_root_protected",
            "The registered source root cannot be deleted or shared as an artifact.",
            403,
        )


def _mutation_result(
    resource,
    source,
    *,
    status: str,
) -> SourceArtifactMutationResult:
    return SourceArtifactMutationResult(
        artifact=SourceArtifact(
            id=resource.id,
            name=resource.name,
            type=ARTIFACT_TYPES.get(resource.mime_type.casefold(), "file"),
        ),
        source=SourceMetadata(
            id=source.context.source_id,
            name=source.context.source_name,
            classification=source.context.classification,
        ),
        status=status,
    )


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


def inspect_authorized_source_artifacts(
    *,
    registry: SourceRegistry,
    source_policy: SourceAccessPolicy,
    workspace_adapter: GoogleWorkspaceAdapter,
    source_id: str,
    limit: int = 100,
) -> tuple[SourceDefinition, list[ArtifactMetadata]]:
    """Inspect safe artifact metadata within one registered, readable source."""

    source = registered_source(registry, source_id)
    allowed_source = source_policy.authorize_source(source)
    safe_limit = DriveReadPolicy.validate_list_limit(limit)
    artifacts = workspace_adapter.inspect_source_artifacts(
        source=allowed_source,
        limit=safe_limit,
    )
    return source, artifacts


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
