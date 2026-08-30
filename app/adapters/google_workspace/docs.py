"""Controlled Google Docs retrieval and allowlisted structured mutation."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Iterator
from typing import Any

from google.auth.exceptions import GoogleAuthError
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.adapters.google_workspace.auth import build_delegated_credentials
from app.adapters.google_workspace.errors import WorkspaceAdapterError, map_google_error
from app.adapters.google_workspace.models import GoogleDocContent, WorkspaceResource
from app.artifact_refs import ArtifactReferenceCodec
from app.config.settings import Settings
from app.document_production import (
    PLACEHOLDER_PATTERN,
    CreateFooterOperation,
    CreateHeaderOperation,
    DeleteContentOperation,
    DeleteTableRowOperation,
    DocumentEditOperation,
    DocumentStructure,
    InsertTableColumnOperation,
    InsertTableOperation,
    InsertTableRowOperation,
    InsertTextOperation,
    ListOperation,
    ParagraphStyleOperation,
    ParagraphSummary,
    ReplaceAllTextOperation,
    SectionSummary,
    SegmentSummary,
    TableCellStyleOperation,
    TableCellSummary,
    TableSummary,
    TabStructure,
    TextStyleOperation,
    TextStyleSummary,
    UpdateTableCellOperation,
)
from app.visual_assets import (
    GoogleDocImageOperation,
    GoogleDocImageSummary,
    InsertGoogleDocImageOperation,
    ReplaceGoogleDocImageOperation,
)

GOOGLE_DOC_MIME_TYPE = "application/vnd.google-apps.document"


class GoogleDocsAdapter:
    def __init__(
        self,
        settings: Settings,
        *,
        credentials_factory: Callable[[Settings], Any] = build_delegated_credentials,
        service_builder: Callable[..., Any] = build,
    ) -> None:
        self._settings = settings
        self._credentials_factory = credentials_factory
        self._service_builder = service_builder

    @property
    def max_chars(self) -> int:
        return self._settings.workspace_doc_max_chars

    def get_document(
        self, resource: WorkspaceResource, *, max_chars: int
    ) -> GoogleDocContent:
        try:
            credentials = self._credentials_factory(self._settings)
            docs = self._service_builder(
                "docs", "v1", credentials=credentials, cache_discovery=False
            )
            if resource.mime_type != GOOGLE_DOC_MIME_TYPE:
                raise WorkspaceAdapterError(
                    "resource_type_invalid",
                    "The requested resource is not a native Google Doc.",
                    422,
                )
            document = (
                docs.documents()
                .get(documentId=resource.id, includeTabsContent=True)
                .execute()
            )
            text, truncated = _bounded_text(_document_text_chunks(document), max_chars)
            return GoogleDocContent(
                id=resource.id,
                name=resource.name,
                mime_type=resource.mime_type,
                modified_time=resource.modified_time,
                text=text,
                truncated=truncated,
                limit=max_chars,
            )
        except WorkspaceAdapterError:
            raise
        except (GoogleAuthError, HttpError) as error:
            raise map_google_error(error) from error
        except OSError as error:
            raise WorkspaceAdapterError(
                "credentials_unavailable",
                "Application Default Credentials are not available to the runtime.",
                503,
            ) from error

    def append_text(self, resource: WorkspaceResource, *, text: str) -> None:
        """Apply the legacy bounded append operation retained for compatibility."""

        if resource.mime_type != GOOGLE_DOC_MIME_TYPE:
            raise WorkspaceAdapterError(
                "resource_type_invalid",
                "The requested resource is not a native Google Doc.",
                422,
            )
        try:
            credentials = self._credentials_factory(self._settings)
            docs = self._service_builder(
                "docs", "v1", credentials=credentials, cache_discovery=False
            )
            (
                docs.documents()
                .batchUpdate(
                    documentId=resource.id,
                    body={
                        "requests": [
                            {
                                "insertText": {
                                    "endOfSegmentLocation": {},
                                    "text": text,
                                }
                            }
                        ]
                    },
                )
                .execute()
            )
        except (GoogleAuthError, HttpError) as error:
            raise map_google_error(error) from error
        except OSError as error:
            raise WorkspaceAdapterError(
                "credentials_unavailable",
                "Application Default Credentials are not available to the runtime.",
                503,
            ) from error

    def inspect_structure(
        self,
        resource: WorkspaceResource,
        *,
        artifact_ref: str,
        source_id: str,
        reference_codec: ArtifactReferenceCodec,
    ) -> DocumentStructure:
        """Return a bounded, allowlisted structural view with actionable indexes."""

        document = self._get_native_document(resource)
        tabs = list(
            _tab_structures(
                document,
                reference_codec=reference_codec,
                source_id=source_id,
                artifact_id=resource.id,
            )
        )
        if not tabs:
            raise WorkspaceAdapterError(
                "document_tabs_unavailable",
                "Google Docs did not return tab metadata for this document.",
                502,
            )
        headers: list[SegmentSummary] = []
        footers: list[SegmentSummary] = []
        for tab in document.get("tabs", []):
            tab_headers, tab_footers = _tab_segment_structures(
                tab,
                reference_codec=reference_codec,
                source_id=source_id,
                artifact_id=resource.id,
            )
            headers.extend(tab_headers)
            footers.extend(tab_footers)
        all_text = "".join(
            paragraph.text
            for tab in tabs
            for paragraph in tab.paragraphs
        ) + "".join(
            paragraph.text
            for segment in (*headers, *footers)
            for paragraph in segment.paragraphs
        )
        style = document.get("documentStyle", {})
        images = list(
            _document_image_summaries(
                document,
                reference_codec=reference_codec,
                source_id=source_id,
                artifact_id=resource.id,
            )
        )
        return DocumentStructure(
            artifact_ref=artifact_ref,
            name=resource.name,
            source_id=source_id,
            revision_id=str(document.get("revisionId", "")),
            tabs=tabs,
            headers=headers,
            footers=footers,
            sections=list(
                _section_summaries(
                    document,
                    reference_codec=reference_codec,
                    source_id=source_id,
                    artifact_id=resource.id,
                )
            ),
            image_count=len(images),
            images=images,
            document_style=_safe_document_style(style),
            placeholders=sorted(set(PLACEHOLDER_PATTERN.findall(all_text)))[:100],
            total_characters=len(all_text),
        )

    def edit_images(
        self,
        resource: WorkspaceResource,
        *,
        required_revision_id: str,
        operations_with_uris: list[tuple[GoogleDocImageOperation, str]],
        tab_id_resolver: Callable[[str], str],
        image_ref_resolver: Callable[[str], tuple[str, str]],
    ) -> str:
        requests: list[dict[str, Any]] = []
        for operation, uri in operations_with_uris:
            if isinstance(operation, InsertGoogleDocImageOperation):
                location: dict[str, Any] = {"index": operation.index}
                if operation.segment_id:
                    location["segmentId"] = operation.segment_id
                if operation.tab_ref:
                    location["tabId"] = tab_id_resolver(operation.tab_ref)
                request: dict[str, Any] = {"uri": uri, "location": location}
                size: dict[str, Any] = {}
                if operation.width_points is not None:
                    size["width"] = {"magnitude": operation.width_points, "unit": "PT"}
                if operation.height_points is not None:
                    size["height"] = {"magnitude": operation.height_points, "unit": "PT"}
                if size:
                    request["objectSize"] = size
                requests.append({"insertInlineImage": request})
            elif isinstance(operation, ReplaceGoogleDocImageOperation):
                object_id, tab_id = image_ref_resolver(operation.image_ref)
                request = {
                    "imageObjectId": object_id,
                    "uri": uri,
                    "imageReplaceMethod": operation.replace_method,
                }
                if tab_id:
                    request["tabId"] = tab_id
                requests.append({"replaceImage": request})
        return self._batch_update(
            resource, requests=requests, required_revision_id=required_revision_id
        )

    def edit_structure(
        self,
        resource: WorkspaceResource,
        *,
        required_revision_id: str,
        operations: list[DocumentEditOperation],
        tab_id_resolver: Callable[[str], str],
    ) -> str:
        """Execute semantic requests with optimistic concurrency protection."""

        if resource.mime_type != GOOGLE_DOC_MIME_TYPE:
            raise WorkspaceAdapterError(
                "resource_type_invalid",
                "The requested resource is not a native Google Doc.",
                422,
            )
        requests: list[dict[str, Any]] = []
        for operation in operations:
            requests.extend(
                _operation_requests(operation, tab_id_resolver=tab_id_resolver)
            )
        return self._batch_update(
            resource,
            requests=requests,
            required_revision_id=required_revision_id,
        )

    def create_tab(
        self,
        resource: WorkspaceResource,
        *,
        title: str,
        required_revision_id: str,
        index: int | None = None,
        parent_tab_id: str | None = None,
    ) -> str:
        properties: dict[str, Any] = {"title": title}
        if index is not None:
            properties["index"] = index
        if parent_tab_id is not None:
            properties["parentTabId"] = parent_tab_id
        return self._batch_update(
            resource,
            requests=[{"addDocumentTab": {"tabProperties": properties}}],
            required_revision_id=required_revision_id,
        )

    def rename_tab(
        self,
        resource: WorkspaceResource,
        *,
        tab_id: str,
        title: str,
        required_revision_id: str,
    ) -> str:
        return self._batch_update(
            resource,
            requests=[
                {
                    "updateDocumentTabProperties": {
                        "tabProperties": {"tabId": tab_id, "title": title},
                        "fields": "title",
                    }
                }
            ],
            required_revision_id=required_revision_id,
        )

    def delete_tab(
        self,
        resource: WorkspaceResource,
        *,
        tab_id: str,
        required_revision_id: str,
    ) -> str:
        return self._batch_update(
            resource,
            requests=[{"deleteTab": {"tabId": tab_id}}],
            required_revision_id=required_revision_id,
        )

    def _batch_update(
        self,
        resource: WorkspaceResource,
        *,
        requests: list[dict[str, Any]],
        required_revision_id: str,
    ) -> str:
        if resource.mime_type != GOOGLE_DOC_MIME_TYPE:
            raise WorkspaceAdapterError(
                "resource_type_invalid",
                "The requested resource is not a native Google Doc.",
                422,
            )
        try:
            credentials = self._credentials_factory(self._settings)
            docs = self._service_builder(
                "docs", "v1", credentials=credentials, cache_discovery=False
            )
            response = (
                docs.documents()
                .batchUpdate(
                    documentId=resource.id,
                    body={
                        "requests": requests,
                        "writeControl": {"requiredRevisionId": required_revision_id},
                    },
                )
                .execute()
            )
            revision = response.get("writeControl", {}).get("requiredRevisionId")
            if revision:
                return str(revision)
            return str(self._get_native_document(resource).get("revisionId", ""))
        except HttpError as error:
            status = getattr(error.resp, "status", None)
            message = str(error).casefold()
            if status in (400, 409) and "revision" in message:
                raise WorkspaceAdapterError(
                    "document_revision_conflict",
                    "The document changed after inspection; inspect it again before mutating it.",
                    409,
                ) from error
            raise map_google_error(error) from error
        except GoogleAuthError as error:
            raise map_google_error(error) from error
        except OSError as error:
            raise WorkspaceAdapterError(
                "credentials_unavailable",
                "Application Default Credentials are not available to the runtime.",
                503,
            ) from error

    def _get_native_document(self, resource: WorkspaceResource) -> dict[str, Any]:
        if resource.mime_type != GOOGLE_DOC_MIME_TYPE:
            raise WorkspaceAdapterError(
                "resource_type_invalid",
                "The requested resource is not a native Google Doc.",
                422,
            )
        try:
            credentials = self._credentials_factory(self._settings)
            docs = self._service_builder(
                "docs", "v1", credentials=credentials, cache_discovery=False
            )
            return (
                docs.documents()
                .get(documentId=resource.id, includeTabsContent=True)
                .execute()
            )
        except (GoogleAuthError, HttpError) as error:
            raise map_google_error(error) from error
        except OSError as error:
            raise WorkspaceAdapterError(
                "credentials_unavailable",
                "Application Default Credentials are not available to the runtime.",
                503,
            ) from error


def _document_text_chunks(document: dict[str, Any]) -> Iterator[str]:
    tabs = document.get("tabs", [])
    if tabs:
        for tab in tabs:
            yield from _tab_text_chunks(tab)
        return
    yield from _structural_text_chunks(document.get("body", {}).get("content", []))


def _tab_text_chunks(tab: dict[str, Any]) -> Iterator[str]:
    document_tab = tab.get("documentTab", {})
    yield from _structural_text_chunks(
        document_tab.get("body", {}).get("content", [])
    )
    for child in tab.get("childTabs", []):
        yield from _tab_text_chunks(child)


def _structural_text_chunks(elements: Iterable[dict[str, Any]]) -> Iterator[str]:
    for element in elements:
        paragraph = element.get("paragraph")
        if paragraph:
            for paragraph_element in paragraph.get("elements", []):
                content = paragraph_element.get("textRun", {}).get("content")
                if content:
                    yield content
        table = element.get("table")
        if table:
            for row in table.get("tableRows", []):
                for cell in row.get("tableCells", []):
                    yield from _structural_text_chunks(cell.get("content", []))
        table_of_contents = element.get("tableOfContents")
        if table_of_contents:
            yield from _structural_text_chunks(table_of_contents.get("content", []))


def _bounded_text(chunks: Iterable[str], limit: int) -> tuple[str, bool]:
    iterator = iter(chunks)
    parts: list[str] = []
    remaining = limit
    for chunk in iterator:
        if len(chunk) > remaining:
            parts.append(chunk[:remaining])
            return "".join(parts), True
        parts.append(chunk)
        remaining -= len(chunk)
        if remaining == 0:
            return "".join(parts), next(iterator, None) is not None
    return "".join(parts), False


def _tab_structures(
    document: dict[str, Any],
    *,
    reference_codec: ArtifactReferenceCodec,
    source_id: str,
    artifact_id: str,
) -> Iterator[TabStructure]:
    for tab in document.get("tabs", []):
        yield from _one_tab_structures(
            tab,
            reference_codec=reference_codec,
            source_id=source_id,
            artifact_id=artifact_id,
        )


def _one_tab_structures(
    tab: dict[str, Any],
    *,
    reference_codec: ArtifactReferenceCodec,
    source_id: str,
    artifact_id: str,
) -> Iterator[TabStructure]:
    properties = tab.get("tabProperties", {})
    tab_id = str(properties.get("tabId", ""))
    if not tab_id:
        raise WorkspaceAdapterError(
            "document_tab_metadata_invalid",
            "Google Docs returned a tab without an immutable identifier.",
            502,
        )
    tab_ref = reference_codec.encode_tab(
        source_id=source_id, artifact_id=artifact_id, tab_id=tab_id
    )
    parent_id = str(properties.get("parentTabId", ""))
    parent_ref = (
        reference_codec.encode_tab(
            source_id=source_id, artifact_id=artifact_id, tab_id=parent_id
        )
        if parent_id
        else None
    )
    document_tab = tab.get("documentTab", {})
    paragraphs, tables = _parse_content(
        document_tab.get("body", {}).get("content", []), tab_ref=tab_ref
    )
    yield TabStructure(
        tab_ref=tab_ref,
        title=str(properties.get("title", "")),
        index=int(properties.get("index", 0)),
        parent_tab_ref=parent_ref,
        nesting_level=int(properties.get("nestingLevel", 0)),
        paragraphs=paragraphs,
        tables=tables,
    )
    for child in tab.get("childTabs", []):
        yield from _one_tab_structures(
            child,
            reference_codec=reference_codec,
            source_id=source_id,
            artifact_id=artifact_id,
        )


def _parse_content(
    content: Iterable[dict[str, Any]], *, tab_ref: str | None, segment_id: str = ""
) -> tuple[list[ParagraphSummary], list[TableSummary]]:
    paragraphs: list[ParagraphSummary] = []
    tables: list[TableSummary] = []
    for element in content:
        paragraph = element.get("paragraph")
        if paragraph:
            paragraphs.append(_paragraph_summary(element, paragraph, tab_ref, segment_id))
        table = element.get("table")
        if table:
            cells: list[TableCellSummary] = []
            rows = table.get("tableRows", [])[:100]
            column_count = 0
            for row_index, row in enumerate(rows):
                row_cells = row.get("tableCells", [])[:50]
                column_count = max(column_count, len(row_cells))
                for column_index, cell in enumerate(row_cells):
                    cell_paragraphs, nested_tables = _parse_content(
                        cell.get("content", []), tab_ref=tab_ref, segment_id=segment_id
                    )
                    paragraphs.extend(cell_paragraphs)
                    tables.extend(nested_tables)
                    cells.append(
                        TableCellSummary(
                            row=row_index,
                            column=column_index,
                            start_index=int(cell.get("startIndex", 0)),
                            end_index=int(cell.get("endIndex", 0)),
                            text="".join(item.text for item in cell_paragraphs)[:10000],
                        )
                    )
            tables.append(
                TableSummary(
                    start_index=int(element.get("startIndex", 0)),
                    end_index=int(element.get("endIndex", 0)),
                    rows=len(rows),
                    columns=column_count,
                    cells=cells,
                    segment_id=segment_id,
                    tab_ref=tab_ref,
                )
            )
    return paragraphs[:1000], tables[:100]


def _paragraph_summary(
    element: dict[str, Any],
    paragraph: dict[str, Any],
    tab_ref: str | None,
    segment_id: str,
) -> ParagraphSummary:
    elements = paragraph.get("elements", [])
    text = "".join(
        str(item.get("textRun", {}).get("content", "")) for item in elements
    )[:10000]
    style = paragraph.get("paragraphStyle", {})
    first_text_style = next(
        (
            item.get("textRun", {}).get("textStyle", {})
            for item in elements
            if item.get("textRun")
        ),
        {},
    )
    return ParagraphSummary(
        start_index=int(element.get("startIndex", 0)),
        end_index=int(element.get("endIndex", 0)),
        text=text,
        named_style_type=style.get("namedStyleType"),
        alignment=style.get("alignment"),
        bullet=bool(paragraph.get("bullet")),
        segment_id=segment_id,
        tab_ref=tab_ref,
        text_style=_text_style_summary(first_text_style),
    )


def _text_style_summary(style: dict[str, Any]) -> TextStyleSummary | None:
    if not style:
        return None
    return TextStyleSummary(
        bold=style.get("bold"),
        italic=style.get("italic"),
        underline=style.get("underline"),
        font_size=style.get("fontSize", {}).get("magnitude"),
        font_family=style.get("weightedFontFamily", {}).get("fontFamily"),
        foreground_color=_color_hex(style.get("foregroundColor")),
    )


def _segment_structures(
    segments: dict[str, Any], *, tab_ref: str | None
) -> list[SegmentSummary]:
    result: list[SegmentSummary] = []
    for segment_id, segment in list(segments.items())[:20]:
        paragraphs, _ = _parse_content(
            segment.get("content", []), tab_ref=tab_ref, segment_id=segment_id
        )
        result.append(
            SegmentSummary(
                segment_id=segment_id, tab_ref=tab_ref, paragraphs=paragraphs
            )
        )
    return result


def _tab_segment_structures(
    tab: dict[str, Any],
    *,
    reference_codec: ArtifactReferenceCodec,
    source_id: str,
    artifact_id: str,
) -> tuple[list[SegmentSummary], list[SegmentSummary]]:
    tab_id = str(tab.get("tabProperties", {}).get("tabId", ""))
    if not tab_id:
        raise WorkspaceAdapterError(
            "document_tab_metadata_invalid",
            "Google Docs returned a tab without an immutable identifier.",
            502,
        )
    tab_ref = reference_codec.encode_tab(
        source_id=source_id, artifact_id=artifact_id, tab_id=tab_id
    )
    document_tab = tab.get("documentTab", {})
    headers = _segment_structures(document_tab.get("headers", {}), tab_ref=tab_ref)
    footers = _segment_structures(document_tab.get("footers", {}), tab_ref=tab_ref)
    for child in tab.get("childTabs", []):
        child_headers, child_footers = _tab_segment_structures(
            child,
            reference_codec=reference_codec,
            source_id=source_id,
            artifact_id=artifact_id,
        )
        headers.extend(child_headers)
        footers.extend(child_footers)
    return headers, footers


def _tab_image_count(tab: dict[str, Any]) -> int:
    document_tab = tab.get("documentTab", {})
    return (
        len(document_tab.get("inlineObjects", {}))
        + len(document_tab.get("positionedObjects", {}))
        + sum(_tab_image_count(child) for child in tab.get("childTabs", []))
    )


def _section_summaries(
    document: dict[str, Any],
    *,
    reference_codec: ArtifactReferenceCodec,
    source_id: str,
    artifact_id: str,
) -> Iterator[SectionSummary]:
    for tab in document.get("tabs", []):
        yield from _tab_section_summaries(
            tab,
            reference_codec=reference_codec,
            source_id=source_id,
            artifact_id=artifact_id,
        )


def _tab_section_summaries(
    tab: dict[str, Any],
    *,
    reference_codec: ArtifactReferenceCodec,
    source_id: str,
    artifact_id: str,
) -> Iterator[SectionSummary]:
    tab_id = str(tab.get("tabProperties", {}).get("tabId", ""))
    if not tab_id:
        raise WorkspaceAdapterError(
            "document_tab_metadata_invalid",
            "Google Docs returned a tab without an immutable identifier.",
            502,
        )
    tab_ref = reference_codec.encode_tab(
        source_id=source_id, artifact_id=artifact_id, tab_id=tab_id
    )
    content = tab.get("documentTab", {}).get("body", {}).get("content", [])
    yield from _sections_from_content(content, tab_ref=tab_ref)
    for child in tab.get("childTabs", []):
        yield from _tab_section_summaries(
            child,
            reference_codec=reference_codec,
            source_id=source_id,
            artifact_id=artifact_id,
        )


def _sections_from_content(
    content: Iterable[dict[str, Any]], *, tab_ref: str | None
) -> Iterator[SectionSummary]:
    for element in content:
        section_break = element.get("sectionBreak")
        if section_break is not None:
            yield SectionSummary(
                start_index=int(element.get("startIndex", 0)),
                end_index=int(element.get("endIndex", 0)),
                tab_ref=tab_ref,
                section_style=_safe_document_style(section_break.get("sectionStyle", {})),
            )


def _safe_document_style(style: dict[str, Any]) -> dict[str, object]:
    allowed = (
        "background",
        "pageSize",
        "marginTop",
        "marginBottom",
        "marginLeft",
        "marginRight",
        "useFirstPageHeaderFooter",
        "flipPageOrientation",
        "sectionType",
        "columnProperties",
        "pageNumberStart",
    )
    return {key: style[key] for key in allowed if key in style}


def _color_hex(color: dict[str, Any] | None) -> str | None:
    rgb = (color or {}).get("color", {}).get("rgbColor", {})
    if not rgb:
        return None
    values = [round(float(rgb.get(key, 0)) * 255) for key in ("red", "green", "blue")]
    return "#" + "".join(f"{value:02X}" for value in values)


def _location(index: int, *, segment_id: str = "", tab_id: str | None = None) -> dict[str, Any]:
    if index == 0 and not segment_id and not tab_id:
        raise WorkspaceAdapterError(
            "document_operation_invalid",
            "Index zero is valid only inside a header, footer, footnote, or explicit tab segment.",
            422,
        )
    result: dict[str, Any] = {"index": index}
    if segment_id:
        result["segmentId"] = segment_id
    if tab_id:
        result["tabId"] = tab_id
    return result


def _resolved_tab_id(
    tab_ref: str | None, *, tab_id_resolver: Callable[[str], str]
) -> str | None:
    return tab_id_resolver(tab_ref) if tab_ref else None


def _range(
    operation: Any, *, tab_id_resolver: Callable[[str], str]
) -> dict[str, Any]:
    if operation.end_index <= operation.start_index:
        raise WorkspaceAdapterError(
            "document_operation_invalid",
            "A document range must end after it starts.",
            422,
        )
    if operation.start_index == 0 and not operation.segment_id:
        raise WorkspaceAdapterError(
            "document_operation_invalid",
            "A range starting at zero requires a header, footer, or footnote segment.",
            422,
        )
    result = {
        "startIndex": operation.start_index,
        "endIndex": operation.end_index,
    }
    if operation.segment_id:
        result["segmentId"] = operation.segment_id
    tab_id = _resolved_tab_id(operation.tab_ref, tab_id_resolver=tab_id_resolver)
    if tab_id:
        result["tabId"] = tab_id
    return result


def _table_cell_location(
    operation: Any, *, tab_id_resolver: Callable[[str], str]
) -> dict[str, Any]:
    start = _location(
        operation.table_start_index,
        tab_id=_resolved_tab_id(operation.tab_ref, tab_id_resolver=tab_id_resolver),
    )
    return {
        "tableStartLocation": start,
        "rowIndex": operation.row_index,
        "columnIndex": operation.column_index,
    }


def _rgb_color(value: str) -> dict[str, Any]:
    return {
        "color": {
            "rgbColor": {
                "red": int(value[1:3], 16) / 255,
                "green": int(value[3:5], 16) / 255,
                "blue": int(value[5:7], 16) / 255,
            }
        }
    }


def _operation_requests(
    operation: DocumentEditOperation,
    *,
    tab_id_resolver: Callable[[str], str] = lambda value: value,
) -> list[dict[str, Any]]:
    tab_id = _resolved_tab_id(
        getattr(operation, "tab_ref", None), tab_id_resolver=tab_id_resolver
    )
    if isinstance(operation, InsertTextOperation):
        return [{"insertText": {"location": _location(operation.index, segment_id=operation.segment_id, tab_id=tab_id), "text": operation.text}}]
    if isinstance(operation, DeleteContentOperation):
        return [{"deleteContentRange": {"range": _range(operation, tab_id_resolver=tab_id_resolver)}}]
    if isinstance(operation, ReplaceAllTextOperation):
        request: dict[str, Any] = {
            "containsText": {"text": operation.find, "matchCase": operation.match_case},
            "replaceText": operation.replace,
        }
        if operation.tab_refs:
            request["tabsCriteria"] = {
                "tabIds": [tab_id_resolver(item) for item in operation.tab_refs]
            }
        return [{"replaceAllText": request}]
    if isinstance(operation, ParagraphStyleOperation):
        style: dict[str, Any] = {}
        fields: list[str] = []
        for field, api_field in (("named_style_type", "namedStyleType"), ("alignment", "alignment")):
            value = getattr(operation, field)
            if value is not None:
                style[api_field] = value
                fields.append(api_field)
        for field, api_field in (("space_above", "spaceAbove"), ("space_below", "spaceBelow")):
            value = getattr(operation, field)
            if value is not None:
                style[api_field] = {"magnitude": value, "unit": "PT"}
                fields.append(api_field)
        if not fields:
            raise WorkspaceAdapterError("document_operation_invalid", "At least one paragraph style is required.", 422)
        return [{"updateParagraphStyle": {"range": _range(operation, tab_id_resolver=tab_id_resolver), "paragraphStyle": style, "fields": ",".join(fields)}}]
    if isinstance(operation, TextStyleOperation):
        style = {}
        fields = []
        for field in ("bold", "italic", "underline"):
            value = getattr(operation, field)
            if value is not None:
                style[field] = value
                fields.append(field)
        if operation.font_size is not None:
            style["fontSize"] = {"magnitude": operation.font_size, "unit": "PT"}
            fields.append("fontSize")
        if operation.font_family is not None:
            style["weightedFontFamily"] = {"fontFamily": operation.font_family}
            fields.append("weightedFontFamily")
        if operation.foreground_color is not None:
            style["foregroundColor"] = _rgb_color(operation.foreground_color)
            fields.append("foregroundColor")
        if not fields:
            raise WorkspaceAdapterError("document_operation_invalid", "At least one text style is required.", 422)
        return [{"updateTextStyle": {"range": _range(operation, tab_id_resolver=tab_id_resolver), "textStyle": style, "fields": ",".join(fields)}}]
    if isinstance(operation, ListOperation):
        preset = "BULLET_DISC_CIRCLE_SQUARE" if operation.list_type == "bullet" else "NUMBERED_DECIMAL_NESTED"
        return [{"createParagraphBullets": {"range": _range(operation, tab_id_resolver=tab_id_resolver), "bulletPreset": preset}}]
    if isinstance(operation, InsertTableOperation):
        return [{"insertTable": {"rows": operation.rows, "columns": operation.columns, "location": _location(operation.index, segment_id=operation.segment_id, tab_id=tab_id)}}]
    if isinstance(operation, InsertTableRowOperation):
        return [{"insertTableRow": {"tableCellLocation": _table_cell_location(operation, tab_id_resolver=tab_id_resolver), "insertBelow": operation.insert_below}}]
    if isinstance(operation, InsertTableColumnOperation):
        return [{"insertTableColumn": {"tableCellLocation": _table_cell_location(operation, tab_id_resolver=tab_id_resolver), "insertRight": operation.insert_right}}]
    if isinstance(operation, DeleteTableRowOperation):
        return [{"deleteTableRow": {"tableCellLocation": _table_cell_location(operation, tab_id_resolver=tab_id_resolver)}}]
    if isinstance(operation, UpdateTableCellOperation):
        return [
            {"deleteContentRange": {"range": _range(operation, tab_id_resolver=tab_id_resolver)}},
            {"insertText": {"location": _location(operation.start_index, segment_id=operation.segment_id, tab_id=tab_id), "text": operation.text}},
        ]
    if isinstance(operation, TableCellStyleOperation):
        style = {}
        fields = []
        if operation.background_color:
            style["backgroundColor"] = _rgb_color(operation.background_color)
            fields.append("backgroundColor")
        if operation.vertical_alignment:
            style["contentAlignment"] = operation.vertical_alignment
            fields.append("contentAlignment")
        if not fields:
            raise WorkspaceAdapterError("document_operation_invalid", "At least one table cell style is required.", 422)
        return [{"updateTableCellStyle": {"tableRange": {"tableCellLocation": _table_cell_location(operation, tab_id_resolver=tab_id_resolver), "rowSpan": 1, "columnSpan": 1}, "tableCellStyle": style, "fields": ",".join(fields)}}]
    if isinstance(operation, (CreateHeaderOperation, CreateFooterOperation)):
        if tab_id and operation.section_index is None:
            raise WorkspaceAdapterError(
                "document_operation_invalid",
                "A tab-scoped header or footer requires an inspected section index.",
                422,
            )
        payload: dict[str, Any] = {
            "type": operation.header_type if isinstance(operation, CreateHeaderOperation) else operation.footer_type
        }
        if operation.section_index is not None:
            payload["sectionBreakLocation"] = _location(
                operation.section_index, tab_id=tab_id
            )
        kind = "createHeader" if isinstance(operation, CreateHeaderOperation) else "createFooter"
        return [{kind: payload}]
    raise WorkspaceAdapterError("document_operation_invalid", "The document operation is not supported.", 422)


def _document_image_summaries(
    document: dict[str, Any],
    *,
    reference_codec: ArtifactReferenceCodec,
    source_id: str,
    artifact_id: str,
) -> Iterator[GoogleDocImageSummary]:
    collections = [
        ("", None, document.get("inlineObjects", {}), "inlineObjectProperties", "inline"),
        ("", None, document.get("positionedObjects", {}), "positionedObjectProperties", "positioned"),
    ]
    for tab in document.get("tabs", []):
        tab_id = str(tab.get("tabProperties", {}).get("tabId", ""))
        tab_ref = (
            reference_codec.encode_tab(
                source_id=source_id, artifact_id=artifact_id, tab_id=tab_id
            )
            if tab_id
            else None
        )
        document_tab = tab.get("documentTab", {})
        collections.extend(
            [
                (tab_id, tab_ref, document_tab.get("inlineObjects", {}), "inlineObjectProperties", "inline"),
                (tab_id, tab_ref, document_tab.get("positionedObjects", {}), "positionedObjectProperties", "positioned"),
            ]
        )
    seen: set[tuple[str, str]] = set()
    for tab_id, tab_ref, objects, property_name, kind in collections:
        for object_id, value in objects.items():
            key = (tab_id, str(object_id))
            if key in seen:
                continue
            seen.add(key)
            embedded = value.get(property_name, {}).get("embeddedObject", {})
            size = embedded.get("size", {})
            yield GoogleDocImageSummary(
                image_ref=reference_codec.encode_document_image(
                    source_id=source_id,
                    artifact_id=artifact_id,
                    object_id=str(object_id),
                    tab_id=tab_id,
                ),
                kind=kind,
                tab_ref=tab_ref,
                width_points=_dimension_magnitude(size.get("width")),
                height_points=_dimension_magnitude(size.get("height")),
                positioned_layout=value.get(property_name, {})
                .get("positioning", {})
                .get("layout"),
            )


def _dimension_magnitude(value: object) -> float | None:
    if not isinstance(value, dict):
        return None
    try:
        return float(value.get("magnitude"))
    except (TypeError, ValueError):
        return None
