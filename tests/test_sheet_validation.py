from unittest.mock import Mock

import pytest
from test_sheets_adapter import _resource, _settings

from app.adapters.google_workspace.errors import WorkspaceAdapterError
from app.adapters.google_workspace.models import SheetRangeContent
from app.adapters.google_workspace.sheets import GoogleSheetsAdapter


def fixture(rules, *, start_row=22, start_column=0):
    provider = Mock()
    provider.spreadsheets.return_value.get.return_value.execute.return_value = {
        "sheets": [
            {
                "properties": {
                    "sheetId": 7,
                    "title": "Register",
                    "gridProperties": {"rowCount": 100, "columnCount": 20},
                },
                "data": [
                    {
                        "startRow": start_row,
                        "startColumn": start_column,
                        "rowData": [
                            {
                                "values": [
                                    {"dataValidation": r} if r else {} for r in rules
                                ]
                            }
                        ],
                    }
                ],
            }
        ]
    }
    adapter = GoogleSheetsAdapter(
        _settings(),
        credentials_factory=Mock(),
        service_builder=Mock(return_value=provider),
    )
    adapter.get_structure = Mock(
        return_value={
            "sheets": [
                {
                    "sheet_id": "8",
                    "title": "Allowed's",
                    "row_count": 5,
                    "column_count": 2,
                },
                {
                    "sheet_id": "7",
                    "title": "Register",
                    "row_count": 100,
                    "column_count": 20,
                },
            ]
        }
    )
    read = Mock(
        return_value=SheetRangeContent(
            spreadsheet_id=_resource().id,
            range="'Allowed''s'!A1:A5",
            values=[["Active"], [""], ["Paused"], ["Active"]],
        )
    )
    return adapter, provider, read


def rule(kind="ONE_OF_RANGE", value="='Allowed''s'!$A$1:$A$5", **kwargs):
    return {
        "condition": {"type": kind, "values": [{"userEnteredValue": value}]},
        **kwargs,
    }


def test_no_validation_and_trimmed_empty_cells_are_explicit():
    adapter, provider, read = fixture([])
    result = adapter.get_validation(
        _resource(), range_name="Register!A23:C23", read_source=read
    )
    assert result["cells"] == [
        {"cell": f"{c}23", "has_validation": False} for c in "ABC"
    ]
    read.assert_not_called()
    args = provider.spreadsheets.return_value.get.call_args.kwargs
    assert args["ranges"] == ["Register!A23:C23"]
    assert "dataValidation" in args["fields"]
    assert "userEnteredValue" not in args["fields"]
    provider.spreadsheets.return_value.batchUpdate.assert_not_called()


@pytest.mark.parametrize(
    "strict,semantics", [(True, "reject_input"), (False, "warning")]
)
def test_explicit_list_strict_warning_and_help(strict, semantics):
    adapter, _, read = fixture(
        [rule("ONE_OF_LIST", "Active", strict=strict, inputMessage="Choose status")]
    )
    cell = adapter.get_validation(
        _resource(), range_name="Register!A23", read_source=read
    )["cells"][0]
    assert cell["allowed_values"] == ["Active"]
    assert cell["strict"] is strict
    assert cell["input_semantics"] == semantics
    assert cell["help_text"] == "Choose status"
    read.assert_not_called()


@pytest.mark.parametrize(
    "expression", ["='Allowed''s'!$A$1:$A$5", "='Allowed''s'!A:A", "='Allowed''s'!A1:A"]
)
def test_range_resolution_provenance_and_shared_read(expression):
    adapter, _, read = fixture([rule(value=expression), rule(value=expression)])
    result = adapter.get_validation(
        _resource(), range_name="Register!A23:B23", read_source=read
    )
    source = result["validation_sources"][0]
    assert source["resolved_values"] == ["Active", "Paused"]
    assert source["sheet_id"] == "8"
    assert source["sheet_title"] == "Allowed's"
    assert source["source_range"] == expression
    assert all(c["domain_status"] == "resolved" for c in result["cells"])
    read.assert_called_once_with(source["resolved_range"])


def test_empty_domain_is_resolved_not_unresolved():
    adapter, _, read = fixture([rule()])
    read.return_value.values = []
    source = adapter.get_validation(
        _resource(), range_name="Register!A23", read_source=read
    )["validation_sources"][0]
    assert source["status"] == "resolved"
    assert source["resolved_values"] == []


def test_inaccessible_source_is_not_guessed():
    adapter, _, read = fixture([rule()])
    read.side_effect = WorkspaceAdapterError("resource_access_denied", "Denied", 403)
    result = adapter.get_validation(
        _resource(), range_name="Register!A23", read_source=read
    )
    assert result["cells"][0]["domain_status"] == "unresolved"
    assert result["validation_sources"][0]["resolved_values"] is None
    assert result["validation_sources"][0]["error_code"] == "resource_access_denied"


@pytest.mark.parametrize(
    "expression",
    [
        "=Missing!A1:A5",
        "=NamedRange",
        '=INDIRECT("A1:A5")',
        "=https://example.com!A1:A5",
        "=Register!A1:B100",
    ],
)
def test_unsupported_missing_or_oversized_source_never_read(expression):
    adapter, _, read = fixture([rule(value=expression)])
    result = adapter.get_validation(
        _resource(), range_name="Register!A23", read_source=read
    )
    assert result["cells"][0]["domain_status"] == "unresolved"
    read.assert_not_called()


@pytest.mark.parametrize("target", ["A:A", "A1:C100", "A0", "NamedRange", "B2:A1"])
def test_invalid_or_unbounded_targets_rejected_before_google(target):
    adapter, provider, read = fixture([])
    with pytest.raises(WorkspaceAdapterError):
        adapter.get_validation(_resource(), range_name=target, read_source=read)
    provider.spreadsheets.assert_not_called()


def test_outside_grid_is_not_reported_as_no_validation():
    adapter, _, read = fixture([])
    with pytest.raises(WorkspaceAdapterError):
        adapter.get_validation(_resource(), range_name="A101", read_source=read)


def test_aggregate_source_budget():
    adapter, _, read = fixture(
        [rule(value="=Register!A1:A60"), rule(value="=Register!B1:B60")]
    )
    result = adapter.get_validation(
        _resource(), range_name="Register!A23:B23", read_source=read
    )
    assert [s["status"] for s in result["validation_sources"]] == [
        "resolved",
        "unresolved",
    ]
    read.assert_called_once()


def test_other_criteria_preserve_operands_without_inventing_domain():
    adapter, _, read = fixture([rule("CUSTOM_FORMULA", "=A23>0")])
    cell = adapter.get_validation(
        _resource(), range_name="Register!A23", read_source=read
    )["cells"][0]
    assert cell["criterion"] == "CUSTOM_FORMULA"
    assert cell["condition_values"] == [{"userEnteredValue": "=A23>0"}]
    assert cell["domain_status"] == "not_enumerated"
    assert cell["allowed_values"] is None
    read.assert_not_called()
