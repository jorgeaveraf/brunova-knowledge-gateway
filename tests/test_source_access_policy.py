import pytest

from app.adapters.google_workspace.errors import WorkspaceAdapterError
from app.adapters.google_workspace.models import WorkspaceResource
from app.config.settings import Settings
from app.policies.source_access import SourceAccessPolicy
from app.source_registry import SourceDefinition, SourceRegistry, SourceRegistryDocument


def settings(*, blocked=()):
    return Settings(
        workspace_delegated_user="reader@example.com",
        workspace_service_account_email="gateway@project.iam.gserviceaccount.com",
        workspace_doc_max_chars=1000,
        workspace_sheet_max_cells=100,
        workspace_blocked_source_ids=blocked,
        workspace_source_max_depth=20,
        workspace_audit_enabled=True,
        workspace_source_registry_path="unused.yaml",
    )


def registry(*, location_type="folder", status="active", classification="management_only"):
    source = SourceDefinition.model_validate(
        {
            "id": "career_ops",
            "name": "Career Ops",
            "system": "google_workspace",
            "location_type": location_type,
            "location_id": (
                "drive_123456789"
                if location_type == "shared_drive"
                else "folder_123456789"
            ),
            "classification": classification,
            "owner": ["Jorge", "Nat"],
            "status": status,
        }
    )
    return SourceRegistry(SourceRegistryDocument(version=1, sources=(source,)))


def resource(*, drive_id=None, ancestors=("folder_123456789",)):
    return WorkspaceResource(
        id="document_123456789",
        name="Document",
        mime_type="application/vnd.google-apps.document",
        modified_time="2026-08-22T00:00:00Z",
        drive_id=drive_id,
        ancestor_ids=ancestors,
    )


def test_resource_in_registered_folder_is_authorized_and_classified():
    policy = SourceAccessPolicy(settings(), registry())

    context = policy.authorize(resource())

    assert context.source_id == "career_ops"
    assert context.classification.value == "management_only"


def test_registered_source_is_explicitly_authorized():
    source_registry = registry()
    policy = SourceAccessPolicy(settings(), source_registry)

    allowed = policy.authorize_source(source_registry.get("career_ops"))

    assert allowed.definition.id == "career_ops"
    assert allowed.context.classification.value == "management_only"


def test_resource_in_registered_shared_drive_is_authorized():
    policy = SourceAccessPolicy(settings(), registry(location_type="shared_drive"))

    context = policy.authorize(resource(drive_id="drive_123456789"))

    assert context.source_id == "career_ops"


def test_blocked_source_takes_precedence_over_registry():
    policy = SourceAccessPolicy(
        settings(blocked=("document_123456789",)), registry()
    )

    with pytest.raises(WorkspaceAdapterError) as error:
        policy.authorize(resource())

    assert error.value.code == "source_not_allowed"


def test_empty_registry_fails_closed():
    empty = SourceRegistry(SourceRegistryDocument(version=1, sources=()))
    policy = SourceAccessPolicy(settings(), empty)

    with pytest.raises(WorkspaceAdapterError) as error:
        policy.allowed_sources()

    assert error.value.code == "source_policy_unconfigured"


def test_disabled_source_is_rejected():
    policy = SourceAccessPolicy(settings(), registry(status="disabled"))

    with pytest.raises(WorkspaceAdapterError) as error:
        policy.authorize(resource())

    assert error.value.code == "source_disabled"


def test_explicitly_selected_blocked_source_is_rejected():
    source_registry = registry()
    policy = SourceAccessPolicy(
        settings(blocked=("folder_123456789",)), source_registry
    )

    with pytest.raises(WorkspaceAdapterError) as error:
        policy.authorize_source(source_registry.get("career_ops"))

    assert error.value.code == "source_not_allowed"


def test_resource_must_belong_to_explicitly_selected_source():
    source_registry = registry()
    policy = SourceAccessPolicy(settings(), source_registry)
    selected = policy.authorize_source(source_registry.get("career_ops"))

    with pytest.raises(WorkspaceAdapterError) as error:
        policy.authorize_resource_for_source(
            resource(ancestors=("another_folder_123",)),
            selected,
        )

    assert error.value.code == "resource_not_in_source"


def test_resource_in_explicitly_selected_source_is_authorized():
    source_registry = registry()
    policy = SourceAccessPolicy(settings(), source_registry)
    selected = policy.authorize_source(source_registry.get("career_ops"))

    context = policy.authorize_resource_for_source(resource(), selected)

    assert context.source_id == "career_ops"


def test_resource_outside_registry_is_rejected():
    policy = SourceAccessPolicy(settings(), registry())

    with pytest.raises(WorkspaceAdapterError) as error:
        policy.authorize(resource(ancestors=("another_folder_123",)))

    assert error.value.code == "source_not_allowed"


def test_archive_destination_is_not_readable_as_a_knowledge_source():
    archive = SourceDefinition.model_validate(
        {
            "id": "legacy_archive",
            "name": "98 Legacy",
            "system": "google_workspace",
            "location_type": "folder",
            "location_id": "archive_folder_123",
            "classification": "management_only",
            "owner": ["Management"],
            "status": "active",
            "source_type": "archive_destination",
            "capabilities": {"read": False, "move": True},
        }
    )
    source_registry = SourceRegistry(
        SourceRegistryDocument(version=1, sources=(archive,))
    )
    policy = SourceAccessPolicy(settings(), source_registry)

    with pytest.raises(WorkspaceAdapterError) as error:
        policy.authorize_source(archive)

    assert error.value.code == "source_not_readable"
    assert policy.authorize_source(archive, require_read=False).definition == archive
    with pytest.raises(WorkspaceAdapterError) as implicit_error:
        policy.authorize(
            resource(ancestors=("archive_folder_123",))
        )
    assert implicit_error.value.code == "source_not_allowed"


def test_validation_source_is_explicitly_readable_but_not_aggregated():
    validation = SourceDefinition.model_validate(
        {
            "id": "workspace_validation",
            "name": "Validation Sandbox",
            "system": "google_workspace",
            "location_type": "folder",
            "location_id": "validation_folder_123",
            "classification": "management_only",
            "owner": ["Management"],
            "status": "active",
            "source_type": "validation_source",
            "capabilities": {"read": True, "create": True, "update": True},
        }
    )
    productive = registry().get("career_ops")
    source_registry = SourceRegistry(
        SourceRegistryDocument(version=1, sources=(productive, validation))
    )
    policy = SourceAccessPolicy(settings(), source_registry)

    assert policy.authorize_source(validation).definition == validation
    assert policy.authorize(
        resource(ancestors=("validation_folder_123",))
    ).source_id == "workspace_validation"
    assert [item.definition.id for item in policy.allowed_sources().folders] == [
        "career_ops"
    ]
