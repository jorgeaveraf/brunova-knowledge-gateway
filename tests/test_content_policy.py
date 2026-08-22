import pytest
from fastapi import HTTPException

from app.policies.workspace import ContentReadPolicy


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
