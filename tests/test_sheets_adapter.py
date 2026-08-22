from unittest.mock import Mock

from app.adapters.google_workspace.models import WorkspaceResource
from app.adapters.google_workspace.sheets import GoogleSheetsAdapter
from app.config.settings import Settings


def test_sheets_adapter_reads_only_the_requested_range():
    settings = Settings(
        workspace_delegated_user="reader@example.com",
        workspace_service_account_email="gateway@project.iam.gserviceaccount.com",
        workspace_doc_max_chars=1000,
        workspace_sheet_max_cells=100,
        workspace_blocked_source_ids=(),
        workspace_source_max_depth=20,
        workspace_audit_enabled=True,
        workspace_source_registry_path="app/config/sources.yaml",
    )
    get_request = Mock()
    get_request.execute.return_value = {"range": "Sheet1!A1:B2", "values": [[1, 2]]}
    values = Mock()
    values.get.return_value = get_request
    spreadsheets = Mock()
    spreadsheets.values.return_value = values
    sheets = Mock()
    sheets.spreadsheets.return_value = spreadsheets
    adapter = GoogleSheetsAdapter(
        settings,
        credentials_factory=Mock(return_value=object()),
        service_builder=Mock(return_value=sheets),
    )

    resource = WorkspaceResource(
        id="spreadsheet_12345",
        name="Data",
        mime_type="application/vnd.google-apps.spreadsheet",
        modified_time="2026-08-22T00:00:00Z",
        drive_id=None,
        ancestor_ids=("allowed_folder_123",),
    )
    result = adapter.get_range(resource, range_name="A1:B2")

    assert result.model_dump() == {
        "spreadsheet_id": "spreadsheet_12345",
        "range": "A1:B2",
        "values": [[1, 2]],
        "request_id": None,
        "source": None,
    }
    values.get.assert_called_once_with(
        spreadsheetId="spreadsheet_12345", range="A1:B2"
    )
