from unittest.mock import Mock

from app.adapters.google_workspace.models import WorkspaceResource
from app.adapters.google_workspace.sheets import GoogleSheetsAdapter
from app.config.settings import Settings
from app.spreadsheet_production import (
    AppendRowsOperation,
    ClearRangeOperation,
    CreateSheetOperation,
    DeleteColumnsOperation,
    DeleteRowsOperation,
    DeleteSheetOperation,
    FormatRangeOperation,
    InsertColumnsOperation,
    InsertRowsOperation,
    RenameSheetOperation,
    SetValuesOperation,
)


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


def _settings():
    return Settings(
        workspace_delegated_user="reader@example.com",
        workspace_service_account_email="gateway@project.iam.gserviceaccount.com",
        workspace_doc_max_chars=1000,
        workspace_sheet_max_cells=100,
        workspace_blocked_source_ids=(),
        workspace_source_max_depth=20,
        workspace_audit_enabled=True,
        workspace_source_registry_path="app/config/sources.yaml",
    )


def _resource():
    return WorkspaceResource(
        id="spreadsheet_12345",
        name="Data",
        mime_type="application/vnd.google-apps.spreadsheet",
        modified_time="",
        drive_id=None,
        ancestor_ids=("allowed_folder_123",),
    )


def test_sheets_adapter_inspects_only_allowlisted_structure():
    request = Mock()
    request.execute.return_value = {
        "properties": {"title": "Forecast", "locale": "en_US", "timeZone": "UTC"},
        "sheets": [
            {
                "properties": {
                    "sheetId": 7,
                    "title": "Summary",
                    "index": 0,
                    "gridProperties": {
                        "rowCount": 100,
                        "columnCount": 12,
                        "frozenRowCount": 1,
                    },
                }
            }
        ],
    }
    spreadsheets = Mock()
    spreadsheets.get.return_value = request
    service = Mock()
    service.spreadsheets.return_value = spreadsheets
    adapter = GoogleSheetsAdapter(
        _settings(), credentials_factory=Mock(return_value=object()), service_builder=Mock(return_value=service)
    )

    result = adapter.get_structure(_resource())

    assert result == {
        "title": "Forecast",
        "locale": "en_US",
        "time_zone": "UTC",
        "sheets": [
            {
                "sheet_id": "7",
                "title": "Summary",
                "index": 0,
                "row_count": 100,
                "column_count": 12,
                "frozen_row_count": 1,
                "frozen_column_count": 0,
            }
        ],
    }
    assert "permissions" not in str(spreadsheets.get.call_args)


def test_sheets_adapter_applies_values_formula_append_and_clear():
    values = Mock()
    spreadsheets = Mock()
    spreadsheets.values.return_value = values
    service = Mock()
    service.spreadsheets.return_value = spreadsheets
    adapter = GoogleSheetsAdapter(
        _settings(), credentials_factory=Mock(return_value=object()), service_builder=Mock(return_value=service)
    )
    operations = [
        SetValuesOperation(operation="set_values", sheet_ref="summary_ref", range="A1:B1", values=[["Total", "=SUM(A2:A10)"]], value_input_option="USER_ENTERED"),
        AppendRowsOperation(operation="append_rows", sheet_ref="summary_ref", range="A1:B20", values=[["A", 1]]),
        ClearRangeOperation(operation="clear_range", sheet_ref="summary_ref", range="B2:B3"),
    ]

    adapter.apply_operations(
        _resource(), operations=operations, sheet_id_resolver=lambda _: 7, sheet_title_resolver=lambda _: "Summary"
    )

    values.update.assert_called_once_with(
        spreadsheetId="spreadsheet_12345", range="'Summary'!A1:B1", valueInputOption="USER_ENTERED", body={"values": [["Total", "=SUM(A2:A10)"]]}
    )
    values.append.assert_called_once_with(
        spreadsheetId="spreadsheet_12345", range="'Summary'!A1:B20", valueInputOption="RAW", insertDataOption="INSERT_ROWS", body={"values": [["A", 1]]}
    )
    values.clear.assert_called_once_with(
        spreadsheetId="spreadsheet_12345", range="'Summary'!B2:B3", body={}
    )


def test_sheets_adapter_qualifies_local_range_and_escapes_sheet_title():
    values = Mock()
    spreadsheets = Mock()
    spreadsheets.values.return_value = values
    service = Mock()
    service.spreadsheets.return_value = spreadsheets
    adapter = GoogleSheetsAdapter(
        _settings(),
        credentials_factory=Mock(return_value=object()),
        service_builder=Mock(return_value=service),
    )

    adapter.apply_operations(
        _resource(),
        operations=[
            ClearRangeOperation(
                operation="clear_range",
                sheet_ref="tasks_ref",
                range="A1:G30",
            )
        ],
        sheet_id_resolver=lambda _: 9,
        sheet_title_resolver=lambda _: "Team's Tasks",
    )

    values.clear.assert_called_once_with(
        spreadsheetId="spreadsheet_12345",
        range="'Team''s Tasks'!A1:G30",
        body={},
    )


def test_sheets_adapter_applies_sheet_dimension_and_basic_format_operations():
    spreadsheets = Mock()
    service = Mock()
    service.spreadsheets.return_value = spreadsheets
    adapter = GoogleSheetsAdapter(
        _settings(), credentials_factory=Mock(return_value=object()), service_builder=Mock(return_value=service)
    )
    operations = [
        CreateSheetOperation(operation="create_sheet", title="Data", row_count=50, column_count=8),
        RenameSheetOperation(operation="rename_sheet", sheet_ref="sheet_ref", title="Overview"),
        InsertRowsOperation(operation="insert_rows", sheet_ref="sheet_ref", start_index=2, count=3),
        DeleteRowsOperation(operation="delete_rows", sheet_ref="sheet_ref", start_index=2, count=1),
        InsertColumnsOperation(operation="insert_columns", sheet_ref="sheet_ref", start_index=1, count=2),
        DeleteColumnsOperation(operation="delete_columns", sheet_ref="sheet_ref", start_index=1, count=1),
        FormatRangeOperation(operation="format_range", sheet_ref="sheet_ref", range="A1:B2", bold=True, italic=True, font_size=12, horizontal_alignment="CENTER", number_format_type="NUMBER", number_format_pattern="0.00", background_color="#112233", text_color="#FFFFFF"),
        DeleteSheetOperation(operation="delete_sheet", sheet_ref="sheet_ref"),
    ]

    adapter.apply_operations(
        _resource(), operations=operations, sheet_id_resolver=lambda _: 7, sheet_title_resolver=lambda _: "Summary"
    )

    spreadsheets.batchUpdate.assert_called_once()
    requests = spreadsheets.batchUpdate.call_args.kwargs["body"]["requests"]
    assert requests[0]["addSheet"]["properties"]["title"] == "Data"
    assert requests[1]["updateSheetProperties"]["properties"] == {"sheetId": 7, "title": "Overview"}
    assert requests[2]["insertDimension"]["range"]["dimension"] == "ROWS"
    assert requests[3]["deleteDimension"]["range"]["dimension"] == "ROWS"
    assert requests[4]["insertDimension"]["range"]["dimension"] == "COLUMNS"
    assert requests[5]["deleteDimension"]["range"]["dimension"] == "COLUMNS"
    assert requests[6]["repeatCell"]["range"] == {"sheetId": 7, "startRowIndex": 0, "endRowIndex": 2, "startColumnIndex": 0, "endColumnIndex": 2}
    assert "userEnteredFormat.textFormat.bold" in requests[6]["repeatCell"]["fields"]
    assert requests[7] == {"deleteSheet": {"sheetId": 7}}
