import pytest
from fastapi import HTTPException
from pydantic import ValidationError

from app.adapters.google_workspace.errors import WorkspaceAdapterError
from app.policies.workspace import ContentReadPolicy, SpreadsheetMutationPolicy
from app.spreadsheet_production import (
    ClearRangeOperation,
    InsertRowsOperation,
    SetValuesOperation,
)


def test_bounded_sheet_range_within_limit_is_allowed():
    assert (
        ContentReadPolicy.validate_sheet_range("'Q1 Data'!A1:F10", max_cells=60)
        == "'Q1 Data'!A1:F10"
    )


@pytest.mark.parametrize("range_name", ["A:F", "1:20", "A1", "A1:", "F20:A1"])
def test_unbounded_or_invalid_sheet_ranges_are_rejected(range_name):
    with pytest.raises(HTTPException) as error:
        ContentReadPolicy.validate_sheet_range(range_name, max_cells=1000)

    assert error.value.status_code == 422


def test_sheet_range_over_cell_limit_is_rejected_with_count():
    with pytest.raises(HTTPException) as error:
        ContentReadPolicy.validate_sheet_range("A1:F20", max_cells=100)

    assert error.value.status_code == 422
    assert "120 cells" in error.value.detail


def _validate(operations, *, max_cells=100):
    SpreadsheetMutationPolicy.validate_operations(
        operations,
        max_cells=max_cells,
        sheet_titles={"Summary"},
        sheet_ref_titles={"sheet_ref": "Summary"},
        sheet_dimensions={"sheet_ref": (100, 20)},
    )


def test_spreadsheet_mutation_policy_accepts_bounded_values_and_dimensions():
    _validate(
        [
            SetValuesOperation(
                operation="set_values", range="Summary!A1:B2", values=[[1, 2], [3, 4]]
            ),
            InsertRowsOperation(
                operation="insert_rows", sheet_ref="sheet_ref", start_index=5, count=10
            ),
        ]
    )


@pytest.mark.parametrize(
    ("operations", "code"),
    [
        ([ClearRangeOperation(operation="clear_range", range="A:A")], "spreadsheet_range_invalid"),
        (
            [
                SetValuesOperation(
                    operation="set_values", range="Summary!A1:J5", values=[[1] * 10 for _ in range(5)]
                ),
                SetValuesOperation(
                    operation="set_values", range="Summary!A6:J10", values=[[1] * 10 for _ in range(5)]
                ),
            ],
            "spreadsheet_cell_limit_exceeded",
        ),
    ],
)
def test_spreadsheet_mutation_policy_rejects_unbounded_payloads(operations, code):
    with pytest.raises(WorkspaceAdapterError) as captured:
        _validate(operations, max_cells=99)
    assert captured.value.code == code


def test_spreadsheet_mutation_policy_rejects_oversized_batch():
    operations = [
        ClearRangeOperation(operation="clear_range", range="Summary!A1:A1")
        for _ in range(SpreadsheetMutationPolicy.MAX_OPERATIONS + 1)
    ]
    with pytest.raises(WorkspaceAdapterError) as captured:
        _validate(operations)
    assert captured.value.code == "spreadsheet_operations_invalid"


def test_spreadsheet_dimension_operation_is_bounded_by_schema():
    with pytest.raises(ValidationError):
        InsertRowsOperation(
            operation="insert_rows", sheet_ref="sheet_ref", start_index=0, count=1001
        )
