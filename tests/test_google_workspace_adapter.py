from unittest.mock import Mock

from app.adapters.google_workspace.drive import GoogleWorkspaceAdapter
from app.config.settings import Settings
from app.policies.source_access import SourceAccessPolicy


def settings():
    return Settings(
        workspace_delegated_user="reader@example.com",
        workspace_service_account_email="gateway@project.iam.gserviceaccount.com",
        workspace_doc_max_chars=10000,
        workspace_sheet_max_cells=1000,
        workspace_allowed_shared_drive_ids=(),
        workspace_allowed_folder_ids=("allowed_folder_123",),
        workspace_blocked_source_ids=(),
        workspace_source_max_depth=20,
        workspace_audit_enabled=True,
    )


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
        limit=5, source_policy=SourceAccessPolicy(settings())
    )

    assert [item.model_dump() for item in result] == [
        {"id": "document_12345", "name": "Plan", "type": "document"},
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
