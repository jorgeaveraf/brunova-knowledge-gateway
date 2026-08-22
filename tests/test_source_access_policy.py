import pytest

from app.adapters.google_workspace.errors import WorkspaceAdapterError
from app.adapters.google_workspace.models import WorkspaceResource
from app.config.settings import Settings
from app.policies.source_access import SourceAccessPolicy


def settings(*, allowed_folders=(), allowed_drives=(), blocked=()):
    return Settings(
        workspace_delegated_user="reader@example.com",
        workspace_service_account_email="gateway@project.iam.gserviceaccount.com",
        workspace_doc_max_chars=1000,
        workspace_sheet_max_cells=100,
        workspace_allowed_shared_drive_ids=allowed_drives,
        workspace_allowed_folder_ids=allowed_folders,
        workspace_blocked_source_ids=blocked,
        workspace_source_max_depth=20,
        workspace_audit_enabled=True,
    )


def resource(*, drive_id=None, ancestors=("folder_123456789",)):
    return WorkspaceResource(
        id="document_123456789",
        name="Document",
        mime_type="application/vnd.google-apps.document",
        modified_time="2026-08-22T00:00:00Z",
        drive_id=drive_id,
        ancestor_ids=ancestors,
    )


def test_resource_in_allowed_folder_is_authorized():
    policy = SourceAccessPolicy(settings(allowed_folders=("folder_123456789",)))

    assert policy.authorize(resource()) is None


def test_resource_in_allowed_shared_drive_is_authorized():
    policy = SourceAccessPolicy(settings(allowed_drives=("drive_123456789",)))

    assert policy.authorize(resource(drive_id="drive_123456789")) is None


def test_blocked_source_takes_precedence_over_allowlist():
    policy = SourceAccessPolicy(
        settings(
            allowed_folders=("folder_123456789",),
            blocked=("document_123456789",),
        )
    )

    with pytest.raises(WorkspaceAdapterError) as error:
        policy.authorize(resource())

    assert error.value.code == "source_not_allowed"
    assert error.value.status_code == 403


def test_empty_allowlist_fails_closed():
    policy = SourceAccessPolicy(settings())

    with pytest.raises(WorkspaceAdapterError) as error:
        policy.authorize(resource())

    assert error.value.code == "source_policy_unconfigured"
    assert error.value.status_code == 503


def test_resource_outside_allowlist_is_rejected():
    policy = SourceAccessPolicy(settings(allowed_folders=("another_folder_123",)))

    with pytest.raises(WorkspaceAdapterError) as error:
        policy.authorize(resource())

    assert error.value.code == "source_not_allowed"


def test_blocked_configured_source_is_removed_from_list_sources():
    policy = SourceAccessPolicy(
        settings(
            allowed_folders=("folder_123456789",),
            blocked=("folder_123456789",),
        )
    )

    with pytest.raises(WorkspaceAdapterError) as error:
        policy.allowed_sources()

    assert error.value.code == "source_policy_unconfigured"
