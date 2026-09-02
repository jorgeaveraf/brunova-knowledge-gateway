"""Semantic Knowledge Gateway operations shared by HTTP and MCP interfaces."""

from __future__ import annotations

import hashlib
from contextlib import ExitStack

from app.adapters.google_workspace.auth import build_keyless_signing_credentials
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
    MARKDOWN_PATTERNS,
    PLACEHOLDER_PATTERN,
    ArtifactReference,
    ArtifactReferenceMutationResult,
    DocumentEditOperation,
    DocumentEditResult,
    DocumentQualityRequirements,
    DocumentQualityResult,
    DocumentStructure,
    DocumentTabInspectionResult,
    DocumentTabMutationResult,
)
from app.docx_production import (
    DOCX_MIME_TYPE,
    DocxEditOperation,
    DocxEditResult,
    DocxPackage,
    DocxRequirements,
    DocxStructure,
    DocxValidationResult,
)
from app.policies.content_mutation import ContentMutationPolicy, MutationOperation
from app.policies.source_access import SourceAccessPolicy
from app.policies.workspace import (
    ContentReadPolicy,
    DriveReadPolicy,
    SpreadsheetMutationPolicy,
)
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
)
from app.source_governance import (
    create_source_proposal as build_source_proposal,
)
from app.source_proposal_store import SourceProposalStore
from app.source_registry import (
    Classification,
    SourceDefinition,
    SourceRegistry,
    SourceRegistryMetadata,
)
from app.spreadsheet_production import (
    SpreadsheetEditOperation,
    SpreadsheetEditResult,
    SpreadsheetQualityRequirements,
    SpreadsheetQualityResult,
    SpreadsheetSheetSummary,
    SpreadsheetStructure,
)
from app.visual_assets import (
    MAX_SOURCE_BYTES,
    AssetTransformationSummary,
    GoogleDocImageEditResult,
    GoogleDocImageOperation,
    ReplaceGoogleDocImageOperation,
    TransientAssetPublisher,
    VisualAssetInspection,
    derived_dimensions,
    docs_points_to_render_pixels,
    inspect_visual_bytes,
    render_for_insertion,
)

SOURCE_CANDIDATE_LOOKUP_LIMIT = 100
GOOGLE_DOC_MIME_TYPE = "application/vnd.google-apps.document"
GOOGLE_SHEET_MIME_TYPE = "application/vnd.google-apps.spreadsheet"
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
    if artifact_type not in {"document", "spreadsheet"}:
        raise WorkspaceAdapterError(
            "mutation_type_not_supported",
            "Only native Google Docs and Google Sheets can be created by this Gateway.",
            422,
        )
    allowed_source = mutation_policy.authorize(
        source_id=source_id,
        operation=MutationOperation.CREATE,
        approval_reference=approval_reference,
    )
    safe_name = mutation_policy.validate_name(name)
    resource = (
        workspace_adapter.create_document(source=allowed_source, name=safe_name)
        if artifact_type == "document"
        else workspace_adapter.create_spreadsheet(source=allowed_source, name=safe_name)
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
    matches = workspace_adapter.find_resources(
        name=resolved_name, mime_type=mime_type, source=allowed_source
    )
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
    return _artifact_reference(reference_codec, source_id, authorized[0])

def inspect_authorized_visual_asset(
    *,
    registry: SourceRegistry,
    source_policy: SourceAccessPolicy,
    workspace_adapter: GoogleWorkspaceAdapter,
    reference_codec: ArtifactReferenceCodec,
    source_id: str,
    artifact_ref: str,
) -> VisualAssetInspection:
    source = registered_source(registry, source_id)
    allowed_source = source_policy.authorize_source(source)
    resource = _resource_from_reference(
        reference_codec, workspace_adapter, source_policy, allowed_source, source_id, artifact_ref
    )
    snapshot = workspace_adapter.download_binary(resource, max_bytes=MAX_SOURCE_BYTES)
    inspected = inspect_visual_bytes(snapshot.content, resource.mime_type)
    recommended_width, recommended_height = derived_dimensions(
        inspected.width, inspected.height, None, None
    )
    requires_downscale = bool(
        inspected.width
        and inspected.height
        and (recommended_width, recommended_height) != (inspected.width, inspected.height)
    )
    return VisualAssetInspection(
        asset_ref=reference_codec.encode_asset(
            source_id=source_id, artifact_id=resource.id, mime_type=inspected.detected_mime_type
        ),
        name=resource.name,
        mime_type=inspected.detected_mime_type,
        width_pixels=inspected.width,
        height_pixels=inspected.height,
        aspect_ratio=(inspected.width / inspected.height if inspected.width and inspected.height else None),
        file_size=snapshot.size,
        source_id=source_id,
        requires_downscale=requires_downscale,
        recommended_derived_width_pixels=recommended_width,
        recommended_derived_height_pixels=recommended_height,
        directly_insertable_in_docs=(
            inspected.detected_mime_type in {"image/png", "image/jpeg"}
            and not requires_downscale
        ),
        rendering_required=(
            inspected.detected_mime_type == "image/svg+xml" or requires_downscale
        ),
    )


def edit_authorized_document_images(
    *,
    mutation_policy: ContentMutationPolicy,
    source_policy: SourceAccessPolicy,
    workspace_adapter: GoogleWorkspaceAdapter,
    docs_adapter: GoogleDocsAdapter,
    reference_codec: ArtifactReferenceCodec,
    source_id: str,
    artifact_ref: str,
    required_revision_id: str,
    operations: list[GoogleDocImageOperation],
    approval_reference: str,
) -> GoogleDocImageEditResult:
    allowed_source = mutation_policy.authorize(
        source_id=source_id, operation=MutationOperation.UPDATE, approval_reference=approval_reference
    )
    if not required_revision_id.strip() or not 1 <= len(operations) <= 10:
        raise WorkspaceAdapterError(
            "document_image_operations_invalid",
            "A revision and between 1 and 10 image operations are required.",
            422,
        )
    document = _resource_from_reference(
        reference_codec, workspace_adapter, source_policy, allowed_source, source_id, artifact_ref
    )
    if document.mime_type != GOOGLE_DOC_MIME_TYPE:
        raise WorkspaceAdapterError("resource_type_invalid", "The target is not a Google Doc.", 422)
    before = docs_adapter.inspect_structure(
        document,
        artifact_ref=artifact_ref,
        source_id=source_id,
        reference_codec=reference_codec,
    )
    publisher = TransientAssetPublisher(
        bucket_name=docs_adapter._settings.workspace_asset_staging_bucket,
        prefix=docs_adapter._settings.workspace_asset_staging_prefix,
        ttl_seconds=docs_adapter._settings.workspace_asset_url_ttl_seconds,
        signing_credentials=build_keyless_signing_credentials(docs_adapter._settings),
    )
    with ExitStack() as stack:
        prepared = []
        transformations = []
        for operation in operations:
            asset_id, claimed_mime = reference_codec.decode_asset(
                operation.asset_ref, source_id=source_id
            )
            asset = workspace_adapter.get_resource(asset_id)
            source_policy.authorize_resource_for_source(asset, allowed_source)
            if asset.mime_type != claimed_mime:
                raise WorkspaceAdapterError(
                    "asset_reference_stale", "The governed asset MIME type has changed.", 409
                )
            snapshot = workspace_adapter.download_binary(asset, max_bytes=MAX_SOURCE_BYTES)
            width = getattr(operation, "width_points", None)
            height = getattr(operation, "height_points", None)
            if isinstance(operation, ReplaceGoogleDocImageOperation):
                target_image_identity = reference_codec.decode_document_image(
                    operation.image_ref,
                    source_id=source_id,
                    artifact_id=document.id,
                )
                replaced = next(
                    (
                        item
                        for item in before.images
                        if reference_codec.decode_document_image(
                            item.image_ref,
                            source_id=source_id,
                            artifact_id=document.id,
                        )
                        == target_image_identity
                    ),
                    None,
                )
                if replaced is not None:
                    width = replaced.width_points
                    height = replaced.height_points
            image = render_for_insertion(
                snapshot.content,
                asset.mime_type,
                width_pixels=docs_points_to_render_pixels(width),
                height_pixels=docs_points_to_render_pixels(height),
            )
            prepared.append((operation, stack.enter_context(publisher.signed_uri(image))))
            transformations.append(
                AssetTransformationSummary(
                    asset_ref=operation.asset_ref,
                    original_mime_type=image.source_mime_type or asset.mime_type,
                    original_width_pixels=image.source_width_pixels,
                    original_height_pixels=image.source_height_pixels,
                    derived_mime_type=image.mime_type,
                    derived_width_pixels=image.width_pixels,
                    derived_height_pixels=image.height_pixels,
                    transformation=image.transformation,
                )
            )
        revision = docs_adapter.edit_images(
            document,
            required_revision_id=required_revision_id.strip(),
            operations_with_uris=prepared,
            tab_id_resolver=lambda ref: reference_codec.decode_tab(
                ref, source_id=source_id, artifact_id=document.id
            ),
            image_ref_resolver=lambda ref: reference_codec.decode_document_image(
                ref, source_id=source_id, artifact_id=document.id
            ),
        )
    after = docs_adapter.inspect_structure(
        document, artifact_ref=artifact_ref, source_id=source_id, reference_codec=reference_codec
    )
    inserted = sum(item.operation == "insert_image" for item in operations)
    if after.revision_id != revision or after.image_count != before.image_count + inserted:
        raise WorkspaceAdapterError(
            "document_image_verification_failed", "Docs image readback did not match the requested mutation.", 502
        )
    return GoogleDocImageEditResult(
        artifact_ref=artifact_ref,
        source_id=source_id,
        revision_id=revision,
        image_count=after.image_count,
        applied_operations=len(operations),
        asset_transformations=transformations,
    )


def inspect_authorized_docx_structure(
    *,
    registry: SourceRegistry,
    source_policy: SourceAccessPolicy,
    workspace_adapter: GoogleWorkspaceAdapter,
    reference_codec: ArtifactReferenceCodec,
    source_id: str,
    artifact_ref: str,
) -> DocxStructure:
    source = registered_source(registry, source_id)
    allowed_source = source_policy.authorize_source(source)
    resource = _resource_from_reference(
        reference_codec, workspace_adapter, source_policy, allowed_source, source_id, artifact_ref
    )
    if resource.mime_type != DOCX_MIME_TYPE:
        raise WorkspaceAdapterError("resource_type_invalid", "The artifact is not a DOCX file.", 422)
    snapshot = workspace_adapter.download_binary(resource)
    package = DocxPackage(snapshot.content)
    result = package.inspect(
        artifact_ref=artifact_ref,
        name=resource.name,
        source_id=source_id,
        artifact_id=resource.id,
        version=snapshot.version,
        codec=reference_codec,
    )
    return result.model_copy(update={"content_hash": hashlib.sha256(snapshot.content).hexdigest()})


def edit_authorized_source_docx(
    *,
    mutation_policy: ContentMutationPolicy,
    source_policy: SourceAccessPolicy,
    workspace_adapter: GoogleWorkspaceAdapter,
    reference_codec: ArtifactReferenceCodec,
    source_id: str,
    artifact_ref: str,
    required_version: str,
    required_content_hash: str,
    operations: list[DocxEditOperation],
    approval_reference: str,
) -> DocxEditResult:
    allowed_source = mutation_policy.authorize(
        source_id=source_id, operation=MutationOperation.UPDATE, approval_reference=approval_reference
    )
    resource = _resource_from_reference(
        reference_codec, workspace_adapter, source_policy, allowed_source, source_id, artifact_ref
    )
    if resource.mime_type != DOCX_MIME_TYPE:
        raise WorkspaceAdapterError("resource_type_invalid", "The artifact is not a DOCX file.", 422)
    snapshot = workspace_adapter.download_binary(resource)
    input_hash = hashlib.sha256(snapshot.content).hexdigest()
    if snapshot.version != required_version.strip() or input_hash != required_content_hash.strip().casefold():
        raise WorkspaceAdapterError(
            "binary_revision_conflict", "The DOCX changed after inspection; no content was overwritten.", 409
        )
    package = DocxPackage(snapshot.content)
    asset_cache = {}

    def resolve_asset(asset_ref: str, width: int | None, height: int | None):
        key = (asset_ref, width, height)
        if key not in asset_cache:
            asset_id, mime = reference_codec.decode_asset(asset_ref, source_id=source_id)
            asset = workspace_adapter.get_resource(asset_id)
            source_policy.authorize_resource_for_source(asset, allowed_source)
            if asset.mime_type != mime:
                raise WorkspaceAdapterError("asset_reference_stale", "The governed asset MIME changed.", 409)
            binary = workspace_adapter.download_binary(asset, max_bytes=10 * 1024 * 1024)
            asset_cache[key] = render_for_insertion(
                binary.content, mime, width_pixels=width, height_pixels=height
            )
        return asset_cache[key]

    package.apply(
        operations,
        anchor_resolver=lambda anchor: reference_codec.decode_docx_anchor(
            anchor, source_id=source_id, artifact_id=resource.id
        ),
        asset_resolver=resolve_asset,
    )
    output = package.to_bytes()
    output_hash = hashlib.sha256(output).hexdigest()
    updated = workspace_adapter.replace_binary(
        resource,
        content=output,
        expected_version=snapshot.version,
        expected_md5_checksum=snapshot.md5_checksum,
    )
    if updated.id != resource.id:
        raise WorkspaceAdapterError("docx_identity_changed", "Drive returned a different artifact identity.", 502)
    readback = workspace_adapter.download_binary(updated)
    verified = DocxPackage(readback.content)
    if hashlib.sha256(readback.content).hexdigest() != output_hash:
        raise WorkspaceAdapterError("docx_verification_failed", "DOCX readback content differs from upload.", 502)
    for operation in operations:
        if operation.operation == "replace_placeholder":
            checks = verified.validate(
                DocxRequirements(forbidden_placeholders=[operation.placeholder])
            )
            if not checks["forbidden_placeholders"]:
                raise WorkspaceAdapterError("docx_verification_failed", "A replaced placeholder remains.", 502)
    return DocxEditResult(
        artifact_ref=artifact_ref,
        source_id=source_id,
        version=readback.version,
        content_hash=output_hash,
        applied_operations=len(operations),
    )


def validate_authorized_docx_structure(
    *,
    registry: SourceRegistry,
    source_policy: SourceAccessPolicy,
    workspace_adapter: GoogleWorkspaceAdapter,
    reference_codec: ArtifactReferenceCodec,
    source_id: str,
    artifact_ref: str,
    requirements: DocxRequirements,
    expected_version: str | None = None,
) -> DocxValidationResult:
    source = registered_source(registry, source_id)
    allowed_source = source_policy.authorize_source(source)
    resource = _resource_from_reference(
        reference_codec, workspace_adapter, source_policy, allowed_source, source_id, artifact_ref
    )
    if resource.mime_type != DOCX_MIME_TYPE:
        raise WorkspaceAdapterError("resource_type_invalid", "The artifact is not a DOCX file.", 422)
    snapshot = workspace_adapter.download_binary(resource)
    checks = DocxPackage(snapshot.content).validate(requirements)
    if expected_version is not None:
        checks["expected_version"] = snapshot.version == expected_version
    return DocxValidationResult(
        artifact_ref=artifact_ref,
        source_id=source_id,
        version=snapshot.version,
        content_hash=hashlib.sha256(snapshot.content).hexdigest(),
        passed=all(checks.values()),
        checks=checks,
    )


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
    if resource.mime_type not in {GOOGLE_DOC_MIME_TYPE, GOOGLE_SHEET_MIME_TYPE}:
        raise WorkspaceAdapterError(
            "resource_type_invalid",
            "Only native Google Docs and Google Sheets can be copied by this operation.",
            422,
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


def inspect_authorized_spreadsheet_structure(
    *,
    registry: SourceRegistry,
    source_policy: SourceAccessPolicy,
    workspace_adapter: GoogleWorkspaceAdapter,
    sheets_adapter: GoogleSheetsAdapter,
    reference_codec: ArtifactReferenceCodec,
    source_id: str,
    artifact_ref: str,
) -> SpreadsheetStructure:
    source = registered_source(registry, source_id)
    allowed_source = source_policy.authorize_source(source)
    resource = _resource_from_reference(
        reference_codec,
        workspace_adapter,
        source_policy,
        allowed_source,
        source_id,
        artifact_ref,
    )
    if resource.mime_type != GOOGLE_SHEET_MIME_TYPE:
        raise WorkspaceAdapterError(
            "resource_type_invalid",
            "The requested resource is not a native Google Sheet.",
            422,
        )
    metadata = sheets_adapter.get_structure(resource)
    return SpreadsheetStructure(
        artifact_ref=artifact_ref,
        name=resource.name,
        source_id=source_id,
        title=str(metadata.get("title", resource.name)),
        locale=metadata.get("locale"),
        time_zone=metadata.get("time_zone"),
        sheets=[
            SpreadsheetSheetSummary(
                sheet_ref=reference_codec.encode_sheet(
                    source_id=source_id,
                    artifact_id=resource.id,
                    sheet_id=str(item["sheet_id"]),
                ),
                title=str(item["title"]),
                index=int(item["index"]),
                row_count=int(item["row_count"]),
                column_count=int(item["column_count"]),
                frozen_row_count=int(item.get("frozen_row_count", 0)),
                frozen_column_count=int(item.get("frozen_column_count", 0)),
            )
            for item in metadata.get("sheets", [])
        ],
    )


def edit_authorized_source_spreadsheet(
    *,
    mutation_policy: ContentMutationPolicy,
    source_policy: SourceAccessPolicy,
    workspace_adapter: GoogleWorkspaceAdapter,
    sheets_adapter: GoogleSheetsAdapter,
    reference_codec: ArtifactReferenceCodec,
    source_id: str,
    artifact_ref: str,
    operations: list[SpreadsheetEditOperation],
    approval_reference: str,
) -> SpreadsheetEditResult:
    allowed_source = mutation_policy.authorize(
        source_id=source_id,
        operation=MutationOperation.UPDATE,
        approval_reference=approval_reference,
    )
    resource = _resource_from_reference(
        reference_codec,
        workspace_adapter,
        source_policy,
        allowed_source,
        source_id,
        artifact_ref,
    )
    if resource.mime_type != GOOGLE_SHEET_MIME_TYPE:
        raise WorkspaceAdapterError(
            "resource_type_invalid",
            "The requested resource is not a native Google Sheet.",
            422,
        )
    metadata = sheets_adapter.get_structure(resource)
    raw_sheets = metadata.get("sheets", [])
    valid_sheet_ids = {str(item["sheet_id"]) for item in raw_sheets}
    refs_to_ids: dict[str, int] = {}
    ref_titles: dict[str, str] = {}
    ref_dimensions: dict[str, tuple[int, int]] = {}
    raw_by_id = {str(item["sheet_id"]): item for item in raw_sheets}
    for operation in operations:
        sheet_ref = getattr(operation, "sheet_ref", None)
        if not sheet_ref:
            continue
        sheet_id = reference_codec.decode_sheet(
            sheet_ref, source_id=source_id, artifact_id=resource.id
        )
        if sheet_id not in valid_sheet_ids:
            raise WorkspaceAdapterError(
                "spreadsheet_sheet_reference_invalid",
                "The sheet reference is invalid for the selected spreadsheet.",
                403,
            )
        refs_to_ids[sheet_ref] = int(sheet_id)
        ref_titles[sheet_ref] = str(raw_by_id[sheet_id]["title"])
        ref_dimensions[sheet_ref] = (
            int(raw_by_id[sheet_id]["row_count"]),
            int(raw_by_id[sheet_id]["column_count"]),
        )
    title_to_id = {str(item["title"]): int(item["sheet_id"]) for item in raw_sheets}
    SpreadsheetMutationPolicy.validate_operations(
        operations,
        max_cells=sheets_adapter.max_cells,
        sheet_titles=set(title_to_id),
        sheet_ref_titles=ref_titles,
        sheet_dimensions=ref_dimensions,
    )
    sheets_adapter.apply_operations(
        resource,
        operations=operations,
        sheet_id_resolver=lambda sheet_ref: refs_to_ids[sheet_ref],
        sheet_title_resolver=lambda sheet_ref: ref_titles[sheet_ref],
    )
    return SpreadsheetEditResult(
        artifact_ref=artifact_ref,
        source_id=source_id,
        applied_operations=len(operations),
    )


def validate_authorized_spreadsheet_structure(
    *,
    registry: SourceRegistry,
    source_policy: SourceAccessPolicy,
    workspace_adapter: GoogleWorkspaceAdapter,
    sheets_adapter: GoogleSheetsAdapter,
    reference_codec: ArtifactReferenceCodec,
    source_id: str,
    artifact_ref: str,
    requirements: SpreadsheetQualityRequirements,
) -> SpreadsheetQualityResult:
    structure = inspect_authorized_spreadsheet_structure(
        registry=registry,
        source_policy=source_policy,
        workspace_adapter=workspace_adapter,
        sheets_adapter=sheets_adapter,
        reference_codec=reference_codec,
        source_id=source_id,
        artifact_ref=artifact_ref,
    )
    artifact_id = reference_codec.decode(artifact_ref, source_id=source_id)
    resource = workspace_adapter.get_resource(artifact_id)
    sheet_by_title = {item.title: item for item in structure.sheets}
    checks: dict[str, bool] = {}
    for title in requirements.expected_sheets:
        checks[f"sheet:{title}"] = title in sheet_by_title
    for dimension in requirements.minimum_dimensions:
        sheet = sheet_by_title.get(dimension.title)
        checks[f"dimensions:{dimension.title}"] = bool(
            sheet
            and sheet.row_count >= dimension.minimum_rows
            and sheet.column_count >= dimension.minimum_columns
        )
    for required in requirements.required_ranges:
        parsed = SpreadsheetMutationPolicy.parse_range(
            required.range, max_cells=sheets_adapter.max_cells
        )
        if parsed.sheet_title and parsed.sheet_title not in sheet_by_title:
            checks[f"range:{required.range}"] = False
            continue
        render_option = "FORMULA" if required.require_formula else "FORMATTED_VALUE"
        values = sheets_adapter.get_range(
            resource,
            range_name=required.range,
            value_render_option=render_option,
        ).values
        flattened = [value for row in values for value in row]
        passed = True
        if required.must_have_values:
            passed = any(str(value).strip() for value in flattened)
        if required.require_formula:
            passed = passed and any(
                isinstance(value, str) and value.startswith("=") for value in flattened
            )
        if required.no_empty_cells:
            passed = passed and len(flattened) == parsed.cell_count and all(
                str(value).strip() for value in flattened
            )
        checks[f"range:{required.range}"] = passed
    for expected in requirements.expected_headers:
        parsed = SpreadsheetMutationPolicy.parse_range(
            expected.range, max_cells=sheets_adapter.max_cells
        )
        if parsed.sheet_title and parsed.sheet_title not in sheet_by_title:
            checks[f"headers:{expected.range}"] = False
            continue
        values = sheets_adapter.get_range(
            resource, range_name=expected.range
        ).values
        actual = [str(value) for value in (values[0] if values else [])]
        checks[f"headers:{expected.range}"] = actual == expected.values
    failures = [name for name, passed in checks.items() if not passed]
    return SpreadsheetQualityResult(
        artifact_ref=artifact_ref,
        source_id=source_id,
        passed=not failures,
        checks=checks,
        failures=failures,
    )


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
    reference_codec: ArtifactReferenceCodec,
    source_id: str,
    artifact_ref: str,
    sheet_ref: str,
    range_name: str,
) -> tuple[SourceDefinition, SheetRangeContent]:
    """Read a local A1 range from an opaque, spreadsheet-bound sheet handle."""

    source = registered_source(registry, source_id)
    allowed_source = source_policy.authorize_source(source)
    resource = _resource_from_reference(
        reference_codec,
        workspace_adapter,
        source_policy,
        allowed_source,
        source_id,
        artifact_ref,
    )
    if resource.mime_type != GOOGLE_SHEET_MIME_TYPE:
        raise WorkspaceAdapterError(
            "resource_type_invalid",
            "The requested resource is not a native Google Sheet.",
            422,
        )
    safe_range = ContentReadPolicy.validate_sheet_range(
        range_name,
        max_cells=sheets_adapter.max_cells,
    )
    parsed = SpreadsheetMutationPolicy.parse_range(
        safe_range, max_cells=sheets_adapter.max_cells
    )
    if parsed.sheet_title:
        raise WorkspaceAdapterError(
            "spreadsheet_range_invalid",
            "Use a local bounded A1 range and select the sheet with sheet_ref.",
            422,
        )
    sheet_id = reference_codec.decode_sheet(
        sheet_ref, source_id=source_id, artifact_id=resource.id
    )
    sheets = sheets_adapter.get_structure(resource).get("sheets", [])
    sheet = next(
        (item for item in sheets if str(item.get("sheet_id")) == sheet_id), None
    )
    if sheet is None:
        raise WorkspaceAdapterError(
            "spreadsheet_sheet_reference_invalid",
            "The sheet reference is invalid for the selected spreadsheet.",
            403,
        )
    title = str(sheet["title"])
    qualified_range = f"'{title.replace(chr(39), chr(39) * 2)}'!{safe_range}"
    result = sheets_adapter.get_range(resource, range_name=qualified_range)
    context = source_policy.authorize_resource_for_source(resource, allowed_source)
    return source, result.model_copy(
        update={
            "range": safe_range,
            "source": SourceMetadata(
                id=context.source_id,
                name=context.source_name,
                classification=context.classification,
            ),
        }
    )


def retrieve_authorized_sheet_range_by_id(
    *,
    registry: SourceRegistry,
    source_policy: SourceAccessPolicy,
    workspace_adapter: GoogleWorkspaceAdapter,
    sheets_adapter: GoogleSheetsAdapter,
    source_id: str,
    spreadsheet_id: str,
    range_name: str,
) -> tuple[SourceDefinition, SheetRangeContent]:
    """Legacy HTTP retrieval path; MCP callers use opaque artifact/sheet refs."""
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
