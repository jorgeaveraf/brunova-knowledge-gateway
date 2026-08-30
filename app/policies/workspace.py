"""Policies applied before calling Google Workspace."""

import re
from dataclasses import dataclass

from fastapi import HTTPException

from app.adapters.google_workspace.errors import WorkspaceAdapterError
from app.spreadsheet_production import SpreadsheetEditOperation


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


@dataclass(frozen=True)
class BoundedSheetRange:
    value: str
    sheet_title: str | None
    start_row: int
    end_row: int
    start_column: int
    end_column: int

    @property
    def cell_count(self) -> int:
        return (self.end_row - self.start_row + 1) * (
            self.end_column - self.start_column + 1
        )


class SpreadsheetMutationPolicy:
    """Bounds semantic Sheets mutations before provider calls are constructed."""

    MAX_OPERATIONS = 50
    MAX_DIMENSION_CHANGE = 1000
    MAX_TAB_MUTATIONS = 10

    @classmethod
    def parse_range(cls, range_name: str, *, max_cells: int) -> BoundedSheetRange:
        try:
            value = ContentReadPolicy.validate_sheet_range(
                range_name, max_cells=max_cells
            )
        except HTTPException as error:
            raise WorkspaceAdapterError(
                "spreadsheet_range_invalid", str(error.detail), 422
            ) from error
        prefix, separator, _ = value.rpartition("!")
        sheet_title = prefix if separator else None
        if sheet_title and sheet_title.startswith("'") and sheet_title.endswith("'"):
            sheet_title = sheet_title[1:-1].replace("''", "'")
        match = ContentReadPolicy.A1_RANGE_PATTERN.fullmatch(value)
        assert match is not None
        start_col, start_row, end_col, end_row = match.groups()
        return BoundedSheetRange(
            value=value,
            sheet_title=sheet_title,
            start_row=int(start_row),
            end_row=int(end_row),
            start_column=ContentReadPolicy._column_number(start_col),
            end_column=ContentReadPolicy._column_number(end_col),
        )

    @classmethod
    def validate_operations(
        cls,
        operations: list[SpreadsheetEditOperation],
        *,
        max_cells: int,
        sheet_titles: set[str],
        sheet_ref_titles: dict[str, str],
        sheet_dimensions: dict[str, tuple[int, int]],
    ) -> None:
        if not operations or len(operations) > cls.MAX_OPERATIONS:
            raise WorkspaceAdapterError(
                "spreadsheet_operations_invalid",
                f"Provide between 1 and {cls.MAX_OPERATIONS} spreadsheet operations.",
                422,
            )
        written_cells = 0
        tab_mutations = 0
        planned_titles = set(sheet_titles)
        deleted_refs: set[str] = set()
        for operation in operations:
            if hasattr(operation, "range"):
                parsed = cls.parse_range(operation.range, max_cells=max_cells)
                if parsed.sheet_title and parsed.sheet_title not in planned_titles:
                    raise WorkspaceAdapterError(
                        "spreadsheet_sheet_not_found",
                        "A range references a sheet that does not exist.",
                        404,
                    )
                if operation.operation in {"set_values", "append_rows"}:
                    rows = operation.values
                    if not rows or any(not isinstance(row, list) for row in rows):
                        raise WorkspaceAdapterError(
                            "spreadsheet_values_invalid",
                            "Spreadsheet values must contain at least one row.",
                            422,
                        )
                    width = max((len(row) for row in rows), default=0)
                    if width < 1 or any(len(row) != width for row in rows):
                        raise WorkspaceAdapterError(
                            "spreadsheet_values_invalid",
                            "Spreadsheet values must be a non-empty rectangular matrix.",
                            422,
                        )
                    if operation.operation == "set_values" and (
                        len(rows) > parsed.end_row - parsed.start_row + 1
                        or width > parsed.end_column - parsed.start_column + 1
                    ):
                        raise WorkspaceAdapterError(
                            "spreadsheet_values_invalid",
                            "Values exceed the explicit target range.",
                            422,
                        )
                    if operation.operation == "append_rows" and width > (
                        parsed.end_column - parsed.start_column + 1
                    ):
                        raise WorkspaceAdapterError(
                            "spreadsheet_values_invalid",
                            "Appended rows exceed the explicit target columns.",
                            422,
                        )
                    written_cells += len(rows) * width
            if (
                hasattr(operation, "sheet_ref")
                and operation.sheet_ref not in sheet_ref_titles
            ):
                raise WorkspaceAdapterError(
                    "spreadsheet_sheet_reference_invalid",
                    "The sheet reference is invalid for the selected spreadsheet.",
                    403,
                )
            if operation.operation == "create_sheet":
                tab_mutations += 1
                normalized = operation.title.casefold()
                if normalized in {item.casefold() for item in planned_titles}:
                    raise WorkspaceAdapterError(
                        "spreadsheet_sheet_title_duplicate",
                        "A sheet with that title already exists.",
                        409,
                    )
                planned_titles.add(operation.title)
            elif operation.operation == "rename_sheet":
                tab_mutations += 1
                previous = sheet_ref_titles[operation.sheet_ref]
                other_titles = planned_titles - {previous}
                if operation.title.casefold() in {item.casefold() for item in other_titles}:
                    raise WorkspaceAdapterError(
                        "spreadsheet_sheet_title_duplicate",
                        "A sheet with that title already exists.",
                        409,
                    )
                planned_titles.discard(previous)
                planned_titles.add(operation.title)
            elif operation.operation == "delete_sheet":
                tab_mutations += 1
                deleted_refs.add(operation.sheet_ref)
                if len(sheet_titles) - len(deleted_refs) < 1:
                    raise WorkspaceAdapterError(
                        "spreadsheet_last_sheet_protected",
                        "A spreadsheet must retain at least one sheet.",
                        422,
                    )
            elif operation.operation in {
                "insert_rows", "delete_rows", "insert_columns", "delete_columns"
            }:
                if operation.count > cls.MAX_DIMENSION_CHANGE:
                    raise WorkspaceAdapterError(
                        "spreadsheet_dimension_limit_exceeded",
                        f"A dimension operation may affect at most {cls.MAX_DIMENSION_CHANGE} rows or columns.",
                        422,
                    )
                rows, columns = sheet_dimensions[operation.sheet_ref]
                current = rows if operation.operation.endswith("rows") else columns
                end = operation.start_index + operation.count
                if operation.operation.startswith("delete") and end > current:
                    raise WorkspaceAdapterError(
                        "spreadsheet_dimension_invalid",
                        "A delete operation exceeds the current sheet dimensions.",
                        422,
                    )
                if operation.operation.startswith("insert") and operation.start_index > current:
                    raise WorkspaceAdapterError(
                        "spreadsheet_dimension_invalid",
                        "An insert operation starts beyond the current sheet dimensions.",
                        422,
                    )
        if written_cells > max_cells:
            raise WorkspaceAdapterError(
                "spreadsheet_cell_limit_exceeded",
                f"The request writes {written_cells} cells; maximum is {max_cells}.",
                422,
            )
        if tab_mutations > cls.MAX_TAB_MUTATIONS:
            raise WorkspaceAdapterError(
                "spreadsheet_tab_limit_exceeded",
                f"A request may create, rename, or delete at most {cls.MAX_TAB_MUTATIONS} sheets.",
                422,
            )
