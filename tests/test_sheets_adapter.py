from unittest.mock import Mock

from app.adapters.google_workspace.sheets import GoogleSheetsAdapter
from app.config.settings import Settings


def test_sheets_adapter_reads_only_the_requested_range():
    settings = Settings(
        workspace_delegated_user="reader@example.com",
        workspace_service_account_email="gateway@project.iam.gserviceaccount.com",
        workspace_doc_max_chars=1000,
        workspace_sheet_max_cells=100,
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

    result = adapter.get_range("spreadsheet_12345", range_name="A1:B2")

    assert result.model_dump() == {
        "spreadsheet_id": "spreadsheet_12345",
        "range": "A1:B2",
        "values": [[1, 2]],
    }
    values.get.assert_called_once_with(
        spreadsheetId="spreadsheet_12345", range="A1:B2"
    )
