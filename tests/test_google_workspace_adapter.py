from unittest.mock import Mock

from app.adapters.google_workspace.drive import GoogleWorkspaceAdapter
from app.adapters.google_workspace.models import WorkspaceResource
from app.config.settings import Settings
from app.policies.source_access import SourceAccessPolicy
from app.source_registry import SourceDefinition, SourceRegistry, SourceRegistryDocument


def settings():
    return Settings(
        workspace_delegated_user="reader@example.com",
        workspace_service_account_email="gateway@project.iam.gserviceaccount.com",
        workspace_doc_max_chars=10000,
        workspace_sheet_max_cells=1000,
        workspace_blocked_source_ids=(),
        workspace_source_max_depth=20,
        workspace_audit_enabled=True,
        workspace_source_registry_path="app/config/sources.yaml",
    )


def source_registry():
    source = SourceDefinition.model_validate(
        {
            "id": "career_ops",
            "name": "Career Ops",
            "system": "google_workspace",
            "location_type": "folder",
            "location_id": "allowed_folder_123",
            "classification": "management_only",
            "owner": ["Jorge", "Nat"],
            "status": "active",
        }
    )
    return SourceRegistry(SourceRegistryDocument(version=1, sources=(source,)))


def test_adapter_builds_drive_client_with_delegated_credentials():
    credentials = object()
    credentials_factory = Mock(return_value=credentials)
    service_builder = Mock()
    adapter = GoogleWorkspaceAdapter(
        settings(),
        credentials_factory=credentials_factory,
        service_builder=service_builder,
    )

    adapter._drive()

    credentials_factory.assert_called_once_with(settings())
    service_builder.assert_called_once_with(
        "drive", "v3", credentials=credentials, cache_discovery=False
    )


def test_list_files_returns_only_normalized_basic_metadata():
    request = Mock()
    request.execute.return_value = {
        "files": [
            {
                "id": "document_12345",
                "name": "Plan",
                "mimeType": "application/vnd.google-apps.document",
                "owners": [{"emailAddress": "must-not-leak@example.com"}],
            },
            {"name": "photo.png", "mimeType": "image/png"},
        ]
    }
    files = Mock()
    files.list.return_value = request
    drive = Mock()
    drive.files.return_value = files
    adapter = GoogleWorkspaceAdapter(
        settings(),
        credentials_factory=Mock(),
        service_builder=Mock(return_value=drive),
    )

    result = adapter.list_files(
        limit=5, source_policy=SourceAccessPolicy(settings(), source_registry())
    )

    assert [item.model_dump() for item in result] == [
        {
            "id": "document_12345",
            "name": "Plan",
            "type": "document",
            "source": {
                "id": "career_ops",
                "name": "Career Ops",
                "classification": "management_only",
            },
        },
    ]
    files.list.assert_called_once_with(
        q="'allowed_folder_123' in parents and trashed=false",
        spaces="drive",
        includeItemsFromAllDrives=True,
        supportsAllDrives=True,
        pageSize=5,
        fields="files(id,name,mimeType)",
        orderBy="modifiedTime desc",
    )


def test_list_source_files_queries_only_the_selected_authorized_source():
    request = Mock()
    request.execute.return_value = {"files": []}
    files = Mock()
    files.list.return_value = request
    drive = Mock()
    drive.files.return_value = files
    adapter = GoogleWorkspaceAdapter(
        settings(),
        credentials_factory=Mock(),
        service_builder=Mock(return_value=drive),
    )
    registry = source_registry()
    policy = SourceAccessPolicy(settings(), registry)
    source = policy.authorize_source(registry.get("career_ops"))

    result = adapter.list_source_files(
        source=source,
        limit=3,
        source_policy=policy,
    )

    assert result == []
    files.list.assert_called_once_with(
        q="'allowed_folder_123' in parents and trashed=false",
        spaces="drive",
        includeItemsFromAllDrives=True,
        supportsAllDrives=True,
        pageSize=3,
        fields="files(id,name,mimeType)",
        orderBy="modifiedTime desc",
    )


def test_inspect_source_artifacts_detects_office_and_native_formats_safely():
    request = Mock()
    request.execute.return_value = {
        "files": [
            {
                "id": "must-not-leak",
                "name": "Roadmap.xlsx",
                "mimeType": (
                    "application/vnd.openxmlformats-officedocument."
                    "spreadsheetml.sheet"
                ),
                "size": "2048",
                "modifiedTime": "2026-08-23T10:00:00Z",
                "owners": [{"emailAddress": "must-not-leak@example.com"}],
            },
            {
                "name": "Forecast.xlsm",
                "mimeType": "application/vnd.ms-excel.sheet.macroEnabled.12",
                "size": "4096",
                "modifiedTime": "2026-08-23T11:00:00Z",
                "permissions": [{"type": "user"}],
            },
            {
                "name": "Policy.docx",
                "mimeType": (
                    "application/vnd.openxmlformats-officedocument."
                    "wordprocessingml.document"
                ),
                "size": "1024",
                "modifiedTime": "2026-08-23T11:30:00Z",
            },
            {
                "name": "Briefing.pptx",
                "mimeType": (
                    "application/vnd.openxmlformats-officedocument."
                    "presentationml.presentation"
                ),
                "size": "8192",
                "modifiedTime": "2026-08-23T11:45:00Z",
            },
            {
                "name": "Operating Charter",
                "mimeType": "application/vnd.google-apps.document",
                "modifiedTime": "2026-08-23T12:00:00Z",
            },
            {"name": "Ignore.pdf", "mimeType": "application/pdf"},
        ]
    }
    files = Mock()
    files.list.return_value = request
    drive = Mock()
    drive.files.return_value = files
    adapter = GoogleWorkspaceAdapter(
        settings(),
        credentials_factory=Mock(),
        service_builder=Mock(return_value=drive),
    )
    registry = source_registry()
    source = SourceAccessPolicy(settings(), registry).authorize_source(
        registry.get("career_ops")
    )

    result = adapter.inspect_source_artifacts(source=source, limit=25)

    assert [item.model_dump() for item in result] == [
        {
            "name": "Roadmap.xlsx",
            "type": "office_artifact",
            "mime_type": (
                "application/vnd.openxmlformats-officedocument."
                "spreadsheetml.sheet"
            ),
            "extension": "xlsx",
            "size": 2048,
            "modified_time": "2026-08-23T10:00:00Z",
            "source_id": "career_ops",
        },
        {
            "name": "Forecast.xlsm",
            "type": "office_artifact",
            "mime_type": "application/vnd.ms-excel.sheet.macroEnabled.12",
            "extension": "xlsm",
            "size": 4096,
            "modified_time": "2026-08-23T11:00:00Z",
            "source_id": "career_ops",
        },
        {
            "name": "Policy.docx",
            "type": "office_artifact",
            "mime_type": (
                "application/vnd.openxmlformats-officedocument."
                "wordprocessingml.document"
            ),
            "extension": "docx",
            "size": 1024,
            "modified_time": "2026-08-23T11:30:00Z",
            "source_id": "career_ops",
        },
        {
            "name": "Briefing.pptx",
            "type": "office_artifact",
            "mime_type": (
                "application/vnd.openxmlformats-officedocument."
                "presentationml.presentation"
            ),
            "extension": "pptx",
            "size": 8192,
            "modified_time": "2026-08-23T11:45:00Z",
            "source_id": "career_ops",
        },
        {
            "name": "Operating Charter",
            "type": "native_artifact",
            "mime_type": "application/vnd.google-apps.document",
            "extension": None,
            "size": None,
            "modified_time": "2026-08-23T12:00:00Z",
            "source_id": "career_ops",
        },
    ]
    assert all(
        set(item.model_dump())
        == {
            "name",
            "type",
            "mime_type",
            "extension",
            "size",
            "modified_time",
            "source_id",
        }
        for item in result
    )
    files.list.assert_called_once_with(
        q="'allowed_folder_123' in parents and trashed=false",
        spaces="drive",
        includeItemsFromAllDrives=True,
        supportsAllDrives=True,
        pageSize=25,
        fields="files(name,mimeType,size,modifiedTime)",
        orderBy="modifiedTime desc",
    )


def test_discovery_detects_unregistered_shared_drives_and_root_folders():
    shared_request = Mock()
    shared_request.execute.return_value = {
        "drives": [
            {"id": "finance_drive_123", "name": "Finance"},
            {"id": "registered_drive_123", "name": "Registered"},
        ]
    }
    drives = Mock()
    drives.list.return_value = shared_request
    folder_request = Mock()
    folder_request.execute.return_value = {
        "files": [{"id": "sales_folder_123", "name": "Sales"}]
    }
    files = Mock()
    files.list.return_value = folder_request
    drive = Mock()
    drive.drives.return_value = drives
    drive.files.return_value = files
    adapter = GoogleWorkspaceAdapter(
        settings(),
        credentials_factory=Mock(),
        service_builder=Mock(return_value=drive),
    )

    result = adapter.discover_source_candidates(
        excluded_location_ids=frozenset({"registered_drive_123"}),
        limit=5,
    )

    assert [(item.name, item.location_type) for item in result] == [
        ("Finance", "shared_drive"),
        ("Sales", "folder"),
    ]
    assert all(
        item.classification_suggestion.value == "management_only" for item in result
    )
    drives.list.assert_called_once_with(pageSize=5, fields="drives(id,name)")
    files.list.assert_called_once_with(
        q=(
            "'root' in parents and "
            "mimeType='application/vnd.google-apps.folder' and "
            "trashed=false"
        ),
        spaces="drive",
        pageSize=4,
        fields="files(id,name)",
        orderBy="name",
    )


def test_create_document_targets_only_authorized_source_root():
    create_request = Mock()
    create_request.execute.return_value = {
        "id": "created_document_123",
        "name": "Controlled Template",
        "mimeType": "application/vnd.google-apps.document",
        "parents": ["allowed_folder_123"],
    }
    files = Mock()
    files.create.return_value = create_request
    drive = Mock()
    drive.files.return_value = files
    adapter = GoogleWorkspaceAdapter(
        settings(),
        credentials_factory=Mock(),
        service_builder=Mock(return_value=drive),
    )
    registry = source_registry()
    allowed = SourceAccessPolicy(settings(), registry).authorize_source(
        registry.get("career_ops")
    )

    created = adapter.create_document(source=allowed, name="Controlled Template")

    assert created.id == "created_document_123"
    assert created.parent_ids == ("allowed_folder_123",)
    files.create.assert_called_once_with(
        body={
            "name": "Controlled Template",
            "mimeType": "application/vnd.google-apps.document",
            "parents": ["allowed_folder_123"],
        },
        fields="id,name,mimeType,modifiedTime,driveId,parents",
        supportsAllDrives=True,
    )


def test_move_resource_uses_explicit_parent_transition():
    update_request = Mock()
    update_request.execute.return_value = {
        "id": "document_12345",
        "name": "Controlled Template",
        "mimeType": "application/vnd.google-apps.document",
        "parents": ["destination_folder_123"],
    }
    files = Mock()
    files.update.return_value = update_request
    drive = Mock()
    drive.files.return_value = files
    adapter = GoogleWorkspaceAdapter(
        settings(),
        credentials_factory=Mock(),
        service_builder=Mock(return_value=drive),
    )
    resource = WorkspaceResource(
        id="document_12345",
        name="Controlled Template",
        mime_type="application/vnd.google-apps.document",
        modified_time="",
        drive_id=None,
        ancestor_ids=("allowed_folder_123",),
        parent_ids=("old_folder_123",),
    )
    destination = WorkspaceResource(
        id="destination_folder_123",
        name="Validated",
        mime_type="application/vnd.google-apps.folder",
        modified_time="",
        drive_id=None,
        ancestor_ids=("allowed_folder_123",),
        parent_ids=("allowed_folder_123",),
    )

    moved = adapter.move_resource(resource=resource, destination=destination)

    assert moved.parent_ids == ("destination_folder_123",)
    files.update.assert_called_once_with(
        fileId="document_12345",
        addParents="destination_folder_123",
        removeParents="old_folder_123",
        fields="id,name,mimeType,modifiedTime,driveId,parents",
        supportsAllDrives=True,
    )


def test_delete_resource_moves_artifact_to_trash():
    update_request = Mock()
    update_request.execute.return_value = {
        "id": "document_12345",
        "name": "Controlled Template",
        "mimeType": "application/vnd.google-apps.document",
        "parents": ["allowed_folder_123"],
    }
    files = Mock()
    files.update.return_value = update_request
    drive = Mock()
    drive.files.return_value = files
    adapter = GoogleWorkspaceAdapter(
        settings(),
        credentials_factory=Mock(),
        service_builder=Mock(return_value=drive),
    )
    resource = WorkspaceResource(
        id="document_12345",
        name="Controlled Template",
        mime_type="application/vnd.google-apps.document",
        modified_time="",
        drive_id=None,
        ancestor_ids=("allowed_folder_123",),
        parent_ids=("allowed_folder_123",),
    )

    deleted = adapter.delete_resource(resource=resource)

    assert deleted.id == "document_12345"
    assert deleted.ancestor_ids == ("allowed_folder_123",)
    files.update.assert_called_once_with(
        fileId="document_12345",
        body={"trashed": True},
        fields="id,name,mimeType,modifiedTime,driveId,parents",
        supportsAllDrives=True,
    )


def test_share_resource_grants_reader_to_one_explicit_user_without_notification():
    permission_request = Mock()
    permission_request.execute.return_value = {
        "id": "permission_123",
        "type": "user",
        "role": "reader",
        "emailAddress": "reviewer@brunova.mx",
    }
    permissions = Mock()
    permissions.create.return_value = permission_request
    drive = Mock()
    drive.permissions.return_value = permissions
    adapter = GoogleWorkspaceAdapter(
        settings(),
        credentials_factory=Mock(),
        service_builder=Mock(return_value=drive),
    )
    resource = WorkspaceResource(
        id="document_12345",
        name="Controlled Template",
        mime_type="application/vnd.google-apps.document",
        modified_time="",
        drive_id=None,
        ancestor_ids=("allowed_folder_123",),
        parent_ids=("allowed_folder_123",),
    )

    shared = adapter.share_resource(
        resource=resource,
        audience="reviewer@brunova.mx",
    )

    assert shared == resource
    permissions.create.assert_called_once_with(
        fileId="document_12345",
        body={
            "type": "user",
            "role": "reader",
            "emailAddress": "reviewer@brunova.mx",
        },
        fields="id,type,role,emailAddress",
        sendNotificationEmail=False,
        supportsAllDrives=True,
    )
