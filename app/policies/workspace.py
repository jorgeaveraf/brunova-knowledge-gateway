"""Policies applied before calling Google Workspace."""

import re

from fastapi import HTTPException


class DriveReadPolicy:
    MAX_LIST_FILES = 100

    @classmethod
    def validate_list_limit(cls, requested: int) -> int:
        if requested > cls.MAX_LIST_FILES:
            raise HTTPException(
                status_code=422,
                detail=f"limit must be at most {cls.MAX_LIST_FILES}",
            )
        return requested


class ContentReadPolicy:
    """Stricter validation for content access than metadata listing."""

    RESOURCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{10,200}$")
    A1_RANGE_PATTERN = re.compile(
        r"^(?:(?:'(?:[^']|'')+'|[^!]+)!)?"
        r"\$?([A-Za-z]{1,3})\$?([1-9]\d*):"
        r"\$?([A-Za-z]{1,3})\$?([1-9]\d*)$"
    )

    @classmethod
    def validate_resource_id(cls, resource_id: str) -> str:
        if not cls.RESOURCE_ID_PATTERN.fullmatch(resource_id):
            raise HTTPException(
                status_code=422,
                detail="resource id has an invalid format",
            )
        return resource_id

    @staticmethod
    def validate_document_limit(max_chars: int) -> int:
        if max_chars < 1:
            raise HTTPException(
                status_code=503,
                detail="WORKSPACE_DOC_MAX_CHARS must be positive",
            )
        return max_chars

    @classmethod
    def validate_sheet_range(cls, range_name: str, *, max_cells: int) -> str:
        candidate = range_name.strip()
        match = cls.A1_RANGE_PATTERN.fullmatch(candidate)
        if not match:
            raise HTTPException(
                status_code=422,
                detail="range must be a bounded A1 range such as A1:F20",
            )

        start_col, start_row, end_col, end_row = match.groups()
        start_col_number = cls._column_number(start_col)
        end_col_number = cls._column_number(end_col)
        start_row_number = int(start_row)
        end_row_number = int(end_row)

        if end_col_number < start_col_number or end_row_number < start_row_number:
            raise HTTPException(
                status_code=422,
                detail="range end must not precede range start",
            )

        cell_count = (
            (end_col_number - start_col_number + 1)
            * (end_row_number - start_row_number + 1)
        )
        if cell_count > max_cells:
            raise HTTPException(
                status_code=422,
                detail=f"range contains {cell_count} cells; maximum is {max_cells}",
            )
        return candidate

    @staticmethod
    def _column_number(column: str) -> int:
        number = 0
        for character in column.upper():
            number = number * 26 + ord(character) - ord("A") + 1
        return number
