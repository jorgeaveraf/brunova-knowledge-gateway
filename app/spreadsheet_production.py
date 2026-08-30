"""Allowlisted contracts for governed Google Sheets production."""

from __future__ import annotations

from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field, model_validator

SpreadsheetCellValue = str | int | float | bool | None


class SpreadsheetSheetSummary(BaseModel):
    sheet_ref: str
    title: str
    index: int
    row_count: int
    column_count: int
    frozen_row_count: int = 0
    frozen_column_count: int = 0


class SpreadsheetStructure(BaseModel):
    artifact_ref: str
    name: str
    source_id: str
    title: str
    locale: str | None = None
    time_zone: str | None = None
    sheets: list[SpreadsheetSheetSummary]
    concurrency_control: Literal["none"] = "none"


class SetValuesOperation(BaseModel):
    operation: Literal["set_values"]
    range: str
    values: list[list[SpreadsheetCellValue]]
    value_input_option: Literal["RAW", "USER_ENTERED"] = "RAW"


class AppendRowsOperation(BaseModel):
    operation: Literal["append_rows"]
    range: str
    values: list[list[SpreadsheetCellValue]]
    value_input_option: Literal["RAW", "USER_ENTERED"] = "RAW"


class ClearRangeOperation(BaseModel):
    operation: Literal["clear_range"]
    range: str


class CreateSheetOperation(BaseModel):
    operation: Literal["create_sheet"]
    title: str = Field(min_length=1, max_length=100)
    row_count: int = Field(default=1000, ge=1, le=10000)
    column_count: int = Field(default=26, ge=1, le=1000)


class SheetReferenceOperation(BaseModel):
    sheet_ref: str


class RenameSheetOperation(SheetReferenceOperation):
    operation: Literal["rename_sheet"]
    title: str = Field(min_length=1, max_length=100)


class DeleteSheetOperation(SheetReferenceOperation):
    operation: Literal["delete_sheet"]


class DimensionOperation(SheetReferenceOperation):
    start_index: int = Field(ge=0)
    count: int = Field(ge=1, le=1000)


class InsertRowsOperation(DimensionOperation):
    operation: Literal["insert_rows"]


class DeleteRowsOperation(DimensionOperation):
    operation: Literal["delete_rows"]


class InsertColumnsOperation(DimensionOperation):
    operation: Literal["insert_columns"]


class DeleteColumnsOperation(DimensionOperation):
    operation: Literal["delete_columns"]


class FormatRangeOperation(BaseModel):
    operation: Literal["format_range"]
    range: str
    bold: bool | None = None
    italic: bool | None = None
    font_size: int | None = Field(default=None, ge=1, le=100)
    horizontal_alignment: Literal["LEFT", "CENTER", "RIGHT"] | None = None
    number_format_type: Literal[
        "TEXT", "NUMBER", "PERCENT", "CURRENCY", "DATE", "TIME", "DATE_TIME"
    ] | None = None
    number_format_pattern: str | None = Field(default=None, min_length=1, max_length=100)
    background_color: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")
    text_color: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")

    @model_validator(mode="after")
    def require_format(self) -> "FormatRangeOperation":
        if not any(
            value is not None
            for name, value in self.__dict__.items()
            if name not in {"operation", "range", "number_format_pattern"}
        ):
            raise ValueError("At least one formatting property is required.")
        if self.number_format_pattern and not self.number_format_type:
            raise ValueError("number_format_pattern requires number_format_type.")
        return self


SpreadsheetEditOperation = Annotated[
    Union[
        SetValuesOperation,
        AppendRowsOperation,
        ClearRangeOperation,
        CreateSheetOperation,
        RenameSheetOperation,
        DeleteSheetOperation,
        InsertRowsOperation,
        DeleteRowsOperation,
        InsertColumnsOperation,
        DeleteColumnsOperation,
        FormatRangeOperation,
    ],
    Field(discriminator="operation"),
]


class SpreadsheetEditResult(BaseModel):
    artifact_ref: str
    source_id: str
    applied_operations: int
    concurrency_control: Literal["none"] = "none"


class RequiredSpreadsheetRange(BaseModel):
    range: str
    must_have_values: bool = True
    require_formula: bool = False
    no_empty_cells: bool = False


class ExpectedSpreadsheetHeaders(BaseModel):
    range: str
    values: list[str] = Field(min_length=1, max_length=100)


class MinimumSheetDimensions(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    minimum_rows: int = Field(default=1, ge=1, le=10000000)
    minimum_columns: int = Field(default=1, ge=1, le=18278)


class SpreadsheetQualityRequirements(BaseModel):
    model_config = ConfigDict(extra="forbid")

    expected_sheets: list[str] = Field(default_factory=list, max_length=100)
    required_ranges: list[RequiredSpreadsheetRange] = Field(
        default_factory=list, max_length=50
    )
    expected_headers: list[ExpectedSpreadsheetHeaders] = Field(
        default_factory=list, max_length=50
    )
    minimum_dimensions: list[MinimumSheetDimensions] = Field(
        default_factory=list, max_length=100
    )


class SpreadsheetQualityResult(BaseModel):
    artifact_ref: str
    source_id: str
    passed: bool
    checks: dict[str, bool]
    failures: list[str]
