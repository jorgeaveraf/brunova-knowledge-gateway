"""Allowlisted contracts for governed Google Docs production."""

from __future__ import annotations

import re
from typing import Annotated, Literal, Union

from pydantic import BaseModel, ConfigDict, Field


class ArtifactReference(BaseModel):
    artifact_ref: str
    name: str
    type: str
    source_id: str


class ArtifactReferenceMutationResult(BaseModel):
    artifact: ArtifactReference
    status: Literal["copied", "renamed"]


class ArtifactReferenceConversionResult(BaseModel):
    operation: Literal["convert_source_artifact"] = "convert_source_artifact"
    result: Literal["success"] = "success"
    original_artifact: ArtifactReference
    created_artifact: ArtifactReference
    created_artifact_type: Literal[
        "google_document", "google_sheet", "google_presentation"
    ]
    source_id: str


class TextStyleSummary(BaseModel):
    bold: bool | None = None
    italic: bool | None = None
    underline: bool | None = None
    font_size: float | None = None
    font_family: str | None = None
    foreground_color: str | None = None


class ParagraphSummary(BaseModel):
    start_index: int
    end_index: int
    text: str
    named_style_type: str | None = None
    alignment: str | None = None
    bullet: bool = False
    segment_id: str = ""
    tab_ref: str | None = None
    text_style: TextStyleSummary | None = None


class TableCellSummary(BaseModel):
    row: int
    column: int
    start_index: int
    end_index: int
    text: str


class TableSummary(BaseModel):
    start_index: int
    end_index: int
    rows: int
    columns: int
    cells: list[TableCellSummary]
    segment_id: str = ""
    tab_ref: str | None = None


class SegmentSummary(BaseModel):
    segment_id: str
    tab_ref: str | None = None
    paragraphs: list[ParagraphSummary]


class SectionSummary(BaseModel):
    start_index: int
    end_index: int
    tab_ref: str | None = None
    section_style: dict[str, object]


class TabStructure(BaseModel):
    tab_ref: str
    title: str
    index: int
    parent_tab_ref: str | None = None
    nesting_level: int
    paragraphs: list[ParagraphSummary]
    tables: list[TableSummary]


class DocumentTabMutationResult(BaseModel):
    artifact_ref: str
    source_id: str
    revision_id: str
    tab: TabStructure | None = None
    result: Literal["created", "renamed", "deleted"]


class DocumentTabInspectionResult(BaseModel):
    artifact_ref: str
    source_id: str
    revision_id: str
    tab: TabStructure
    headers: list[SegmentSummary]
    footers: list[SegmentSummary]
    sections: list[SectionSummary]
    placeholders: list[str]
    total_characters: int


class DocumentStructure(BaseModel):
    artifact_ref: str
    name: str
    source_id: str
    revision_id: str
    tabs: list[TabStructure]
    headers: list[SegmentSummary]
    footers: list[SegmentSummary]
    sections: list[SectionSummary] = Field(default_factory=list)
    image_count: int
    document_style: dict[str, object]
    placeholders: list[str]
    total_characters: int


class RangeOperation(BaseModel):
    start_index: int = Field(ge=0)
    end_index: int = Field(gt=0)
    segment_id: str = ""
    tab_ref: str | None = None


class InsertTextOperation(BaseModel):
    operation: Literal["insert_text_at_index"]
    index: int = Field(ge=0)
    text: str = Field(min_length=1, max_length=10000)
    segment_id: str = ""
    tab_ref: str | None = None


class DeleteContentOperation(RangeOperation):
    operation: Literal["delete_content_range"]


class ReplaceAllTextOperation(BaseModel):
    operation: Literal["replace_all_text"]
    find: str = Field(min_length=1, max_length=500)
    replace: str = Field(max_length=10000)
    match_case: bool = True
    tab_refs: list[str] | None = None


class ParagraphStyleOperation(RangeOperation):
    operation: Literal["apply_paragraph_style"]
    named_style_type: Literal[
        "TITLE", "SUBTITLE", "HEADING_1", "HEADING_2", "HEADING_3", "NORMAL_TEXT"
    ] | None = None
    alignment: Literal["START", "CENTER", "END", "JUSTIFIED"] | None = None
    space_above: float | None = Field(default=None, ge=0, le=100)
    space_below: float | None = Field(default=None, ge=0, le=100)


class TextStyleOperation(RangeOperation):
    operation: Literal["apply_text_style"]
    bold: bool | None = None
    italic: bool | None = None
    underline: bool | None = None
    font_size: float | None = Field(default=None, ge=1, le=200)
    foreground_color: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")
    font_family: str | None = Field(default=None, min_length=1, max_length=100)


class ListOperation(RangeOperation):
    operation: Literal["create_list"]
    list_type: Literal["bullet", "numbered"]


class InsertTableOperation(BaseModel):
    operation: Literal["insert_table"]
    index: int = Field(ge=1)
    rows: int = Field(ge=1, le=50)
    columns: int = Field(ge=1, le=20)
    segment_id: str = ""
    tab_ref: str | None = None


class TableLocationOperation(BaseModel):
    table_start_index: int = Field(ge=1)
    row_index: int = Field(ge=0)
    column_index: int = Field(ge=0)
    tab_ref: str | None = None


class InsertTableRowOperation(TableLocationOperation):
    operation: Literal["insert_table_row"]
    insert_below: bool = True


class InsertTableColumnOperation(TableLocationOperation):
    operation: Literal["insert_table_column"]
    insert_right: bool = True


class DeleteTableRowOperation(TableLocationOperation):
    operation: Literal["delete_table_row"]


class UpdateTableCellOperation(RangeOperation):
    operation: Literal["update_table_cell_content"]
    text: str = Field(max_length=10000)


class TableCellStyleOperation(TableLocationOperation):
    operation: Literal["apply_table_cell_style"]
    background_color: str | None = Field(default=None, pattern=r"^#[0-9A-Fa-f]{6}$")
    vertical_alignment: Literal["TOP", "MIDDLE", "BOTTOM"] | None = None


class CreateHeaderOperation(BaseModel):
    operation: Literal["create_header"]
    header_type: Literal["DEFAULT"] = "DEFAULT"
    section_index: int | None = Field(default=None, ge=0)
    tab_ref: str | None = None


class CreateFooterOperation(BaseModel):
    operation: Literal["create_footer"]
    footer_type: Literal["DEFAULT"] = "DEFAULT"
    section_index: int | None = Field(default=None, ge=0)
    tab_ref: str | None = None


DocumentEditOperation = Annotated[
    Union[
        InsertTextOperation,
        DeleteContentOperation,
        ReplaceAllTextOperation,
        ParagraphStyleOperation,
        TextStyleOperation,
        ListOperation,
        InsertTableOperation,
        InsertTableRowOperation,
        InsertTableColumnOperation,
        DeleteTableRowOperation,
        UpdateTableCellOperation,
        TableCellStyleOperation,
        CreateHeaderOperation,
        CreateFooterOperation,
    ],
    Field(discriminator="operation"),
]


class DocumentEditResult(BaseModel):
    artifact_ref: str
    source_id: str
    revision_id: str
    applied_operations: int
    result: Literal["success"] = "success"


class DocumentQualityRequirements(BaseModel):
    expected_headings: list[str] = Field(default_factory=list, max_length=50)
    expected_sections: list[str] = Field(default_factory=list, max_length=50)
    minimum_table_count: int = Field(default=0, ge=0, le=100)
    require_header: bool = False
    require_footer: bool = False
    minimum_characters: int = Field(default=1, ge=0)
    reject_placeholders: bool = True
    reject_markdown: bool = True
    tab_requirements: list["DocumentTabQualityRequirements"] = Field(
        default_factory=list, max_length=20
    )
    structural_parity_pairs: list["DocumentTabParityRequirement"] = Field(
        default_factory=list, max_length=10
    )


class DocumentTabQualityRequirements(BaseModel):
    title: str = Field(min_length=1, max_length=100)
    expected_headings: list[str] = Field(default_factory=list, max_length=50)
    expected_sections: list[str] = Field(default_factory=list, max_length=50)
    minimum_table_count: int = Field(default=0, ge=0, le=100)
    require_document_control: bool = False
    document_control_labels: list[str] = Field(default_factory=list, max_length=20)
    require_header: bool = False
    require_footer: bool = False
    minimum_characters: int = Field(default=1, ge=0)
    reject_placeholders: bool = True
    reject_markdown: bool = True


class DocumentTabParityRequirement(BaseModel):
    left_title: str = Field(min_length=1, max_length=100)
    right_title: str = Field(min_length=1, max_length=100)


class DocumentQualityResult(BaseModel):
    artifact_ref: str
    source_id: str
    revision_id: str
    passed: bool
    checks: dict[str, bool]
    issues: list[str]


PLACEHOLDER_PATTERN = re.compile(r"\{\{[^{}\n]{1,100}\}\}")
MARKDOWN_PATTERNS = (
    re.compile(r"(?m)^#{1,6}\s+"),
    re.compile(r"\*\*[^*\n]+\*\*"),
    re.compile(r"```"),
    re.compile(r"\[[^\]\n]+\]\([^\)\n]+\)"),
)
