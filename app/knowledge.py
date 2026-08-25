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
from app.artifact_refs import ArtifactReferenceCodec
from app.document_production import (
    ArtifactReference,
    ArtifactReferenceMutationResult,
    DocumentEditOperation,
    DocumentEditResult,
    DocumentQualityRequirements,
    DocumentQualityResult,
    DocumentStructure,
    DocumentTabInspectionResult,
    DocumentTabMutationResult,
    MARKDOWN_PATTERNS,
    PLACEHOLDER_PATTERN,
)
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

ARTIFACT_TYPE_MIME_TYPES = {
    value: key for key, value in ARTIFACT_TYPES.items()
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


def resolve_authorized_source_artifact(
    *,
    registry: SourceRegistry,
    source_policy: SourceAccessPolicy,
    workspace_adapter: GoogleWorkspaceAdapter,
    reference_codec: ArtifactReferenceCodec,
    source_id: str,
    name: str | None = None,
    logical_path: str | None = None,
    artifact_type: str | None = None,
) -> ArtifactReference:
    """Resolve one exact, authorized resource to an opaque source-bound handle."""

    source = registered_source(registry, source_id)
    allowed_source = source_policy.authorize_source(source)
    supplied_name = (name or "").strip()
    supplied_path = (logical_path or "").strip().strip("/")
    if not supplied_name and not supplied_path:
        raise WorkspaceAdapterError(
            "artifact_selector_invalid",
            "An exact artifact name or logical path is required.",
            422,
        )
    resolved_name = supplied_name or supplied_path.rsplit("/", 1)[-1]
    if supplied_name and supplied_path and supplied_path.rsplit("/", 1)[-1] != supplied_name:
        raise WorkspaceAdapterError(
            "artifact_selector_invalid",
            "The artifact name must match the logical path basename.",
            422,
        )
    mime_type = ARTIFACT_TYPE_MIME_TYPES.get(artifact_type) if artifact_type else None
    if artifact_type and not mime_type:
        raise WorkspaceAdapterError(
            "artifact_type_invalid", "The requested artifact type is unsupported.", 422
        )
    matches = workspace_adapter.find_resources(name=resolved_name, mime_type=mime_type)
    authorized = []
    for resource in matches:
        try:
            source_policy.authorize_resource_for_source(resource, allowed_source)
        except WorkspaceAdapterError:
            continue
        if supplied_path:
            candidate_path = workspace_adapter.logical_path(resource).strip("/")
            if not candidate_path.casefold().endswith(supplied_path.casefold()):
                continue
        authorized.append(resource)
    if not authorized:
        raise WorkspaceAdapterError(
            "artifact_not_found",
            "No artifact matching the selector exists in the selected source.",
            404,
        )
    if len(authorized) != 1:
        raise WorkspaceAdapterError(
            "artifact_selector_ambiguous",
            "The selector matches multiple source artifacts; provide a logical path.",
            409,
        )
    resource = authorized[0]
    return _artifact_reference(reference_codec, source_id, resource)


def copy_authorized_source_artifact(
    *,
    mutation_policy: ContentMutationPolicy,
    source_policy: SourceAccessPolicy,
    workspace_adapter: GoogleWorkspaceAdapter,
    reference_codec: ArtifactReferenceCodec,
    source_id: str,
    artifact_ref: str,
    name: str,
    approval_reference: str,
    destination_ref: str | None = None,
) -> ArtifactReferenceMutationResult:
    allowed_source = mutation_policy.authorize(
        source_id=source_id,
        operation=MutationOperation.CREATE,
        approval_reference=approval_reference,
    )
    resource = _resource_from_reference(
        reference_codec, workspace_adapter, source_policy, allowed_source, source_id, artifact_ref
    )
    if resource.mime_type != GOOGLE_DOC_MIME_TYPE:
        raise WorkspaceAdapterError(
            "resource_type_invalid", "Only native Google Docs can be copied by this operation.", 422
        )
    destination = (
        _resource_from_reference(
            reference_codec,
            workspace_adapter,
            source_policy,
            allowed_source,
            source_id,
            destination_ref,
        )
        if destination_ref
        else workspace_adapter.get_resource(allowed_source.definition.location_id)
    )
    copied = workspace_adapter.copy_resource(
        resource=resource,
        name=mutation_policy.validate_name(name),
        destination=destination,
    )
    return ArtifactReferenceMutationResult(
        artifact=_artifact_reference(reference_codec, source_id, copied), status="copied"
    )


def rename_authorized_source_artifact(
    *,
    mutation_policy: ContentMutationPolicy,
    source_policy: SourceAccessPolicy,
    workspace_adapter: GoogleWorkspaceAdapter,
    reference_codec: ArtifactReferenceCodec,
    source_id: str,
    artifact_ref: str,
    name: str,
    approval_reference: str,
) -> ArtifactReferenceMutationResult:
    allowed_source = mutation_policy.authorize(
        source_id=source_id,
        operation=MutationOperation.UPDATE,
        approval_reference=approval_reference,
    )
    resource = _resource_from_reference(
        reference_codec, workspace_adapter, source_policy, allowed_source, source_id, artifact_ref
    )
    _protect_source_root(resource.id, allowed_source)
    renamed = workspace_adapter.rename_resource(
        resource=resource, name=mutation_policy.validate_name(name)
    )
    return ArtifactReferenceMutationResult(
        artifact=_artifact_reference(reference_codec, source_id, renamed), status="renamed"
    )


def inspect_authorized_document_structure(
    *,
    registry: SourceRegistry,
    source_policy: SourceAccessPolicy,
    workspace_adapter: GoogleWorkspaceAdapter,
    docs_adapter: GoogleDocsAdapter,
    reference_codec: ArtifactReferenceCodec,
    source_id: str,
    artifact_ref: str,
) -> DocumentStructure:
    source = registered_source(registry, source_id)
    allowed_source = source_policy.authorize_source(source)
    resource = _resource_from_reference(
        reference_codec, workspace_adapter, source_policy, allowed_source, source_id, artifact_ref
    )
    return docs_adapter.inspect_structure(
        resource,
        artifact_ref=artifact_ref,
        source_id=source_id,
        reference_codec=reference_codec,
    )


def inspect_authorized_document_tab(
    *,
    registry: SourceRegistry,
    source_policy: SourceAccessPolicy,
    workspace_adapter: GoogleWorkspaceAdapter,
    docs_adapter: GoogleDocsAdapter,
    reference_codec: ArtifactReferenceCodec,
    source_id: str,
    artifact_ref: str,
    tab_ref: str,
) -> DocumentTabInspectionResult:
    structure = inspect_authorized_document_structure(
        registry=registry,
        source_policy=source_policy,
        workspace_adapter=workspace_adapter,
        docs_adapter=docs_adapter,
        reference_codec=reference_codec,
        source_id=source_id,
        artifact_ref=artifact_ref,
    )
    artifact_id = reference_codec.decode(artifact_ref, source_id=source_id)
    target_id = reference_codec.decode_tab(
        tab_ref, source_id=source_id, artifact_id=artifact_id
    )
    tab = _tab_by_internal_id(
        structure, reference_codec, source_id, artifact_id, target_id
    )
    headers = _segments_for_tab(
        structure.headers, reference_codec, source_id, artifact_id, target_id
    )
    footers = _segments_for_tab(
        structure.footers, reference_codec, source_id, artifact_id, target_id
    )
    sections = [
        item
        for item in structure.sections
        if item.tab_ref
        and reference_codec.decode_tab(
            item.tab_ref, source_id=source_id, artifact_id=artifact_id
        )
        == target_id
    ]
    text = "".join(item.text for item in tab.paragraphs)
    return DocumentTabInspectionResult(
        artifact_ref=artifact_ref,
        source_id=source_id,
        revision_id=structure.revision_id,
        tab=tab,
        headers=headers,
        footers=footers,
        sections=sections,
        placeholders=sorted(set(PLACEHOLDER_PATTERN.findall(text)))[:100],
        total_characters=len(text),
    )


def create_authorized_document_tab(
    *,
    mutation_policy: ContentMutationPolicy,
    source_policy: SourceAccessPolicy,
    workspace_adapter: GoogleWorkspaceAdapter,
    docs_adapter: GoogleDocsAdapter,
    reference_codec: ArtifactReferenceCodec,
    source_id: str,
    artifact_ref: str,
    title: str,
    required_revision_id: str,
    approval_reference: str,
    index: int | None = None,
    parent_tab_ref: str | None = None,
) -> DocumentTabMutationResult:
    allowed_source, resource, revision = _authorized_document_tab_mutation(
        mutation_policy=mutation_policy,
        source_policy=source_policy,
        workspace_adapter=workspace_adapter,
        reference_codec=reference_codec,
        source_id=source_id,
        artifact_ref=artifact_ref,
        required_revision_id=required_revision_id,
        approval_reference=approval_reference,
    )
    safe_title = _validated_tab_title(title)
    if index is not None and index < 0:
        raise WorkspaceAdapterError(
            "document_tab_index_invalid",
            "A document tab index must be zero or greater.",
            422,
        )
    current = docs_adapter.inspect_structure(
        resource,
        artifact_ref=artifact_ref,
        source_id=source_id,
        reference_codec=reference_codec,
    )
    _ensure_unique_tab_title(current, safe_title)
    parent_id = (
        reference_codec.decode_tab(
            parent_tab_ref, source_id=source_id, artifact_id=resource.id
        )
        if parent_tab_ref
        else None
    )
    if parent_id:
        _tab_by_internal_id(
            current, reference_codec, source_id, resource.id, parent_id
        )
    docs_adapter.create_tab(
        resource,
        title=safe_title,
        required_revision_id=revision,
        index=index,
        parent_tab_id=parent_id,
    )
    updated = docs_adapter.inspect_structure(
        resource,
        artifact_ref=artifact_ref,
        source_id=source_id,
        reference_codec=reference_codec,
    )
    created = next(tab for tab in updated.tabs if tab.title == safe_title)
    return DocumentTabMutationResult(
        artifact_ref=artifact_ref,
        source_id=allowed_source.definition.id,
        revision_id=updated.revision_id,
        tab=created,
        result="created",
    )


def rename_authorized_document_tab(
    *,
    mutation_policy: ContentMutationPolicy,
    source_policy: SourceAccessPolicy,
    workspace_adapter: GoogleWorkspaceAdapter,
    docs_adapter: GoogleDocsAdapter,
    reference_codec: ArtifactReferenceCodec,
    source_id: str,
    artifact_ref: str,
    tab_ref: str,
    title: str,
    required_revision_id: str,
    approval_reference: str,
) -> DocumentTabMutationResult:
    allowed_source, resource, revision = _authorized_document_tab_mutation(
        mutation_policy=mutation_policy,
        source_policy=source_policy,
        workspace_adapter=workspace_adapter,
        reference_codec=reference_codec,
        source_id=source_id,
        artifact_ref=artifact_ref,
        required_revision_id=required_revision_id,
        approval_reference=approval_reference,
    )
    safe_title = _validated_tab_title(title)
    target_id = reference_codec.decode_tab(
        tab_ref, source_id=source_id, artifact_id=resource.id
    )
    current = docs_adapter.inspect_structure(
        resource,
        artifact_ref=artifact_ref,
        source_id=source_id,
        reference_codec=reference_codec,
    )
    _tab_by_internal_id(current, reference_codec, source_id, resource.id, target_id)
    _ensure_unique_tab_title(
        current,
        safe_title,
        excluded_tab_id=target_id,
        codec=reference_codec,
        source_id=source_id,
        artifact_id=resource.id,
    )
    docs_adapter.rename_tab(
        resource,
        tab_id=target_id,
        title=safe_title,
        required_revision_id=revision,
    )
    updated = docs_adapter.inspect_structure(
        resource,
        artifact_ref=artifact_ref,
        source_id=source_id,
        reference_codec=reference_codec,
    )
    renamed = _tab_by_internal_id(
        updated, reference_codec, source_id, resource.id, target_id
    )
    return DocumentTabMutationResult(
        artifact_ref=artifact_ref,
        source_id=allowed_source.definition.id,
        revision_id=updated.revision_id,
        tab=renamed,
        result="renamed",
    )


def delete_authorized_document_tab(
    *,
    mutation_policy: ContentMutationPolicy,
    source_policy: SourceAccessPolicy,
    workspace_adapter: GoogleWorkspaceAdapter,
    docs_adapter: GoogleDocsAdapter,
    reference_codec: ArtifactReferenceCodec,
    source_id: str,
    artifact_ref: str,
    tab_ref: str,
    required_revision_id: str,
    approval_reference: str,
) -> DocumentTabMutationResult:
    allowed_source, resource, revision = _authorized_document_tab_mutation(
        mutation_policy=mutation_policy,
        source_policy=source_policy,
        workspace_adapter=workspace_adapter,
        reference_codec=reference_codec,
        source_id=source_id,
        artifact_ref=artifact_ref,
        required_revision_id=required_revision_id,
        approval_reference=approval_reference,
    )
    target_id = reference_codec.decode_tab(
        tab_ref, source_id=source_id, artifact_id=resource.id
    )
    current = docs_adapter.inspect_structure(
        resource,
        artifact_ref=artifact_ref,
        source_id=source_id,
        reference_codec=reference_codec,
    )
    _tab_by_internal_id(current, reference_codec, source_id, resource.id, target_id)
    if len(current.tabs) <= 1:
        raise WorkspaceAdapterError(
            "document_tab_delete_invalid",
            "The only document tab cannot be deleted.",
            422,
        )
    for item in current.tabs:
        if item.parent_tab_ref and reference_codec.decode_tab(
            item.parent_tab_ref, source_id=source_id, artifact_id=resource.id
        ) == target_id:
            raise WorkspaceAdapterError(
                "document_tab_delete_has_children",
                "A tab with child tabs cannot be deleted by this governed operation.",
                422,
            )
    docs_adapter.delete_tab(
        resource, tab_id=target_id, required_revision_id=revision
    )
    updated = docs_adapter.inspect_structure(
        resource,
        artifact_ref=artifact_ref,
        source_id=source_id,
        reference_codec=reference_codec,
    )
    return DocumentTabMutationResult(
        artifact_ref=artifact_ref,
        source_id=allowed_source.definition.id,
        revision_id=updated.revision_id,
        tab=None,
        result="deleted",
    )


def _authorized_document_tab_mutation(
    *,
    mutation_policy: ContentMutationPolicy,
    source_policy: SourceAccessPolicy,
    workspace_adapter: GoogleWorkspaceAdapter,
    reference_codec: ArtifactReferenceCodec,
    source_id: str,
    artifact_ref: str,
    required_revision_id: str,
    approval_reference: str,
):
    allowed_source = mutation_policy.authorize(
        source_id=source_id,
        operation=MutationOperation.UPDATE,
        approval_reference=approval_reference,
    )
    revision = required_revision_id.strip()
    if not revision:
        raise WorkspaceAdapterError(
            "document_revision_required", "A required document revision is mandatory.", 422
        )
    resource = _resource_from_reference(
        reference_codec,
        workspace_adapter,
        source_policy,
        allowed_source,
        source_id,
        artifact_ref,
    )
    if resource.mime_type != GOOGLE_DOC_MIME_TYPE:
        raise WorkspaceAdapterError(
            "resource_type_invalid",
            "The requested resource is not a native Google Doc.",
            422,
        )
    return allowed_source, resource, revision


def _validated_tab_title(title: str) -> str:
    value = title.strip()
    if not value or len(value) > 100 or any(ord(character) < 32 for character in value):
        raise WorkspaceAdapterError(
            "document_tab_title_invalid",
            "A document tab title must contain between 1 and 100 visible characters.",
            422,
        )
    return value


def _tab_by_internal_id(
    structure: DocumentStructure,
    reference_codec: ArtifactReferenceCodec,
    source_id: str,
    artifact_id: str,
    tab_id: str,
):
    for tab in structure.tabs:
        if (
            reference_codec.decode_tab(
                tab.tab_ref, source_id=source_id, artifact_id=artifact_id
            )
            == tab_id
        ):
            return tab
    raise WorkspaceAdapterError(
        "document_tab_not_found",
        "The selected tab no longer exists in the document.",
        404,
    )


def _segments_for_tab(
    segments,
    reference_codec: ArtifactReferenceCodec,
    source_id: str,
    artifact_id: str,
    tab_id: str,
):
    return [
        item
        for item in segments
        if item.tab_ref
        and reference_codec.decode_tab(
            item.tab_ref, source_id=source_id, artifact_id=artifact_id
        )
        == tab_id
    ]


def _ensure_unique_tab_title(
    structure: DocumentStructure,
    title: str,
    *,
    excluded_tab_id: str | None = None,
    codec: ArtifactReferenceCodec | None = None,
    source_id: str | None = None,
    artifact_id: str | None = None,
) -> None:
    for tab in structure.tabs:
        if excluded_tab_id is not None:
            if codec is None or source_id is None or artifact_id is None:
                raise RuntimeError("Tab identity context is required for an exclusion.")
            current_id = codec.decode_tab(
                tab.tab_ref, source_id=source_id, artifact_id=artifact_id
            )
            if current_id == excluded_tab_id:
                continue
        if tab.title.casefold() == title.casefold():
            raise WorkspaceAdapterError(
                "document_tab_title_duplicate",
                "A document tab with that title already exists.",
                409,
            )


def _scope_document_operations(
    operations: list[DocumentEditOperation],
    *,
    tab_ref: str | None,
    structure: DocumentStructure,
    reference_codec: ArtifactReferenceCodec,
    source_id: str,
    artifact_id: str,
) -> list[DocumentEditOperation]:
    explicit_scope = tab_ref is not None
    default_ref = tab_ref
    if default_ref is None and len(structure.tabs) == 1:
        default_ref = structure.tabs[0].tab_ref
    default_id = (
        reference_codec.decode_tab(
            default_ref, source_id=source_id, artifact_id=artifact_id
        )
        if default_ref
        else None
    )
    if default_id:
        _tab_by_internal_id(
            structure, reference_codec, source_id, artifact_id, default_id
        )

    scoped: list[DocumentEditOperation] = []
    for operation in operations:
        if hasattr(operation, "tab_refs"):
            references = list(operation.tab_refs or [])
            if not references:
                if default_ref is None:
                    raise WorkspaceAdapterError(
                        "document_tab_reference_required",
                        "Each operation on a multi-tab document requires an opaque tab reference.",
                        422,
                    )
                references = [default_ref]
            resolved = {
                reference_codec.decode_tab(
                    item, source_id=source_id, artifact_id=artifact_id
                )
                for item in references
            }
            if default_id is not None and resolved != {default_id}:
                raise WorkspaceAdapterError(
                    "document_tab_scope_mismatch",
                    "An operation cannot target a tab outside the requested tab scope.",
                    403,
                )
            scoped.append(operation.model_copy(update={"tab_refs": references}))
            continue

        operation_ref = getattr(operation, "tab_ref", None)
        if operation_ref is None:
            if (
                not explicit_scope
                and len(structure.tabs) == 1
                and operation.operation in {"create_header", "create_footer"}
            ):
                scoped.append(operation)
                continue
            if default_ref is None:
                raise WorkspaceAdapterError(
                    "document_tab_reference_required",
                    "Each operation on a multi-tab document requires an opaque tab reference.",
                    422,
                )
            operation_ref = default_ref
        operation_id = reference_codec.decode_tab(
            operation_ref, source_id=source_id, artifact_id=artifact_id
        )
        if default_id is not None and operation_id != default_id:
            raise WorkspaceAdapterError(
                "document_tab_scope_mismatch",
                "An operation cannot target a tab outside the requested tab scope.",
                403,
            )
        scoped.append(operation.model_copy(update={"tab_ref": operation_ref}))
    return scoped


def edit_authorized_source_document(
    *,
    mutation_policy: ContentMutationPolicy,
    source_policy: SourceAccessPolicy,
    workspace_adapter: GoogleWorkspaceAdapter,
    docs_adapter: GoogleDocsAdapter,
    reference_codec: ArtifactReferenceCodec,
    source_id: str,
    artifact_ref: str,
    required_revision_id: str,
    operations: list[DocumentEditOperation],
    approval_reference: str,
    tab_ref: str | None = None,
) -> DocumentEditResult:
    allowed_source = mutation_policy.authorize(
        source_id=source_id,
        operation=MutationOperation.UPDATE,
        approval_reference=approval_reference,
    )
    if not required_revision_id.strip():
        raise WorkspaceAdapterError(
            "document_revision_required", "A required document revision is mandatory.", 422
        )
    if not operations or len(operations) > 50:
        raise WorkspaceAdapterError(
            "document_operations_invalid", "Provide between 1 and 50 semantic operations.", 422
        )
    resource = _resource_from_reference(
        reference_codec, workspace_adapter, source_policy, allowed_source, source_id, artifact_ref
    )
    structure = docs_adapter.inspect_structure(
        resource,
        artifact_ref=artifact_ref,
        source_id=source_id,
        reference_codec=reference_codec,
    )
    scoped_operations = _scope_document_operations(
        operations,
        tab_ref=tab_ref,
        structure=structure,
        reference_codec=reference_codec,
        source_id=source_id,
        artifact_id=resource.id,
    )
    revision = docs_adapter.edit_structure(
        resource,
        required_revision_id=required_revision_id.strip(),
        operations=scoped_operations,
        tab_id_resolver=lambda tab_ref: reference_codec.decode_tab(
            tab_ref, source_id=source_id, artifact_id=resource.id
        ),
    )
    return DocumentEditResult(
        artifact_ref=artifact_ref,
        source_id=source_id,
        revision_id=revision,
        applied_operations=len(scoped_operations),
    )


def validate_authorized_document_structure(
    *,
    registry: SourceRegistry,
    source_policy: SourceAccessPolicy,
    workspace_adapter: GoogleWorkspaceAdapter,
    docs_adapter: GoogleDocsAdapter,
    reference_codec: ArtifactReferenceCodec,
    source_id: str,
    artifact_ref: str,
    requirements: DocumentQualityRequirements,
    expected_revision_id: str | None = None,
) -> DocumentQualityResult:
    structure = inspect_authorized_document_structure(
        registry=registry,
        source_policy=source_policy,
        workspace_adapter=workspace_adapter,
        docs_adapter=docs_adapter,
        reference_codec=reference_codec,
        source_id=source_id,
        artifact_ref=artifact_ref,
    )
    artifact_id = reference_codec.decode(artifact_ref, source_id=source_id)
    paragraphs = [paragraph for tab in structure.tabs for paragraph in tab.paragraphs]
    headings = {
        paragraph.text.strip()
        for paragraph in paragraphs
        if (paragraph.named_style_type or "").startswith("HEADING_")
    }
    full_text = "".join(paragraph.text for paragraph in paragraphs)
    table_count = sum(len(tab.tables) for tab in structure.tabs)
    checks = {
        "unique_tab_titles": len({tab.title.casefold() for tab in structure.tabs})
        == len(structure.tabs),
        "expected_headings": all(item in headings for item in requirements.expected_headings),
        "expected_sections": all(item in headings for item in requirements.expected_sections),
        "minimum_tables": table_count >= requirements.minimum_table_count,
        "header": not requirements.require_header or bool(structure.headers),
        "footer": not requirements.require_footer or bool(structure.footers),
        "minimum_content": structure.total_characters >= requirements.minimum_characters,
        "no_placeholders": not requirements.reject_placeholders or not structure.placeholders,
        "no_markdown": not requirements.reject_markdown or not any(pattern.search(full_text) for pattern in MARKDOWN_PATTERNS),
        "revision": expected_revision_id is None or structure.revision_id == expected_revision_id,
    }
    issue_labels = {
        "unique_tab_titles": "Document tab titles must be unique.",
        "expected_headings": "One or more required headings are missing.",
        "expected_sections": "One or more required sections are missing.",
        "minimum_tables": "The document has fewer tables than required.",
        "header": "A header is required.",
        "footer": "A footer is required.",
        "minimum_content": "The document does not meet the minimum content length.",
        "no_placeholders": "Unresolved placeholders remain.",
        "no_markdown": "Markdown-like formatting remains in the document.",
        "revision": "The document revision does not match the expected revision.",
    }
    tabs_by_title = {tab.title: tab for tab in structure.tabs}
    for requirement in requirements.tab_requirements:
        key_prefix = f"tab:{requirement.title}"
        tab = tabs_by_title.get(requirement.title)
        checks[f"{key_prefix}:exists"] = tab is not None
        issue_labels[f"{key_prefix}:exists"] = (
            f"Required document tab '{requirement.title}' is missing."
        )
        if tab is None:
            continue
        tab_headings = {
            paragraph.text.strip()
            for paragraph in tab.paragraphs
            if (paragraph.named_style_type or "").startswith("HEADING_")
        }
        tab_text = "".join(paragraph.text for paragraph in tab.paragraphs)
        tab_id = reference_codec.decode_tab(
            tab.tab_ref, source_id=source_id, artifact_id=artifact_id
        )
        tab_headers = _segments_for_tab(
            structure.headers, reference_codec, source_id, artifact_id, tab_id
        )
        tab_footers = _segments_for_tab(
            structure.footers, reference_codec, source_id, artifact_id, tab_id
        )
        tab_checks = {
            "expected_headings": all(
                item in tab_headings for item in requirement.expected_headings
            ),
            "expected_sections": all(
                item in tab_headings for item in requirement.expected_sections
            ),
            "minimum_tables": len(tab.tables) >= requirement.minimum_table_count,
            "document_control": not requirement.require_document_control
            or _tab_has_document_control(tab, requirement.document_control_labels),
            "header": not requirement.require_header or bool(tab_headers),
            "footer": not requirement.require_footer or bool(tab_footers),
            "minimum_content": len(tab_text) >= requirement.minimum_characters,
            "no_placeholders": not requirement.reject_placeholders
            or not PLACEHOLDER_PATTERN.search(tab_text),
            "no_markdown": not requirement.reject_markdown
            or not any(pattern.search(tab_text) for pattern in MARKDOWN_PATTERNS),
        }
        tab_issue_text = {
            "expected_headings": "one or more required headings are missing",
            "expected_sections": "one or more required sections are missing",
            "minimum_tables": "fewer tables than required are present",
            "document_control": "Document Control labels are missing",
            "header": "a header is required",
            "footer": "a footer is required",
            "minimum_content": "minimum content length is not met",
            "no_placeholders": "unresolved placeholders remain",
            "no_markdown": "Markdown-like formatting remains",
        }
        for name, passed in tab_checks.items():
            key = f"{key_prefix}:{name}"
            checks[key] = passed
            issue_labels[key] = f"Tab '{requirement.title}': {tab_issue_text[name]}."

    for pair in requirements.structural_parity_pairs:
        key = f"parity:{pair.left_title}:{pair.right_title}"
        left = tabs_by_title.get(pair.left_title)
        right = tabs_by_title.get(pair.right_title)
        checks[key] = (
            left is not None
            and right is not None
            and _tab_structure_signature(left) == _tab_structure_signature(right)
        )
        issue_labels[key] = (
            f"Tabs '{pair.left_title}' and '{pair.right_title}' do not have structural parity."
        )
    return DocumentQualityResult(
        artifact_ref=artifact_ref,
        source_id=source_id,
        revision_id=structure.revision_id,
        passed=all(checks.values()),
        checks=checks,
        issues=[issue_labels[key] for key, passed in checks.items() if not passed],
    )


def _tab_has_document_control(tab, labels: list[str]) -> bool:
    if not labels:
        return False
    normalized = {
        cell.text.strip().casefold()
        for table in tab.tables
        for cell in table.cells
        if cell.text.strip()
    }
    return all(label.strip().casefold() in normalized for label in labels)


def _tab_structure_signature(tab) -> tuple:
    heading_levels = tuple(
        paragraph.named_style_type
        for paragraph in tab.paragraphs
        if (paragraph.named_style_type or "").startswith("HEADING_")
    )
    list_count = sum(1 for paragraph in tab.paragraphs if paragraph.bullet)
    table_shapes = tuple((table.rows, table.columns) for table in tab.tables)
    return heading_levels, list_count, table_shapes


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


def _artifact_reference(
    codec: ArtifactReferenceCodec, source_id: str, resource
) -> ArtifactReference:
    return ArtifactReference(
        artifact_ref=codec.encode(source_id=source_id, artifact_id=resource.id),
        name=resource.name,
        type=ARTIFACT_TYPES.get(resource.mime_type.casefold(), "file"),
        source_id=source_id,
    )


def _resource_from_reference(
    codec: ArtifactReferenceCodec,
    workspace_adapter: GoogleWorkspaceAdapter,
    source_policy: SourceAccessPolicy,
    allowed_source,
    source_id: str,
    artifact_ref: str,
):
    artifact_id = codec.decode(artifact_ref, source_id=source_id)
    resource = workspace_adapter.get_resource(artifact_id)
    source_policy.authorize_resource_for_source(resource, allowed_source)
    return resource


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
