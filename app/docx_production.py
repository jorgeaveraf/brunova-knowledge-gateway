"""Bounded native DOCX contracts and fidelity-oriented OOXML mutation."""

from __future__ import annotations

import hashlib
import io
import posixpath
import re
import zipfile
from copy import deepcopy
from typing import Annotated, Callable, Literal, Union

from lxml import etree
from pydantic import BaseModel, ConfigDict, Field

from app.adapters.google_workspace.errors import WorkspaceAdapterError
from app.artifact_refs import ArtifactReferenceCodec
from app.visual_assets import AssetImage

DOCX_MIME_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
MAX_DOCX_BYTES = 25 * 1024 * 1024
MAX_DOCX_OPERATIONS = 25
MAX_REPLACEMENT_LENGTH = 10_000
MAX_TABLE_MUTATIONS = 20
MAX_PARAGRAPH_MUTATIONS = 20
MAX_INSPECTED_PARAGRAPHS = 250
MAX_INSPECTED_TABLES = 50
MAX_INSPECTED_TEXT = 500

NS = {
    "w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
    "a": "http://schemas.openxmlformats.org/drawingml/2006/main",
    "wp": "http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing",
    "pic": "http://schemas.openxmlformats.org/drawingml/2006/picture",
    "pr": "http://schemas.openxmlformats.org/package/2006/relationships",
    "ct": "http://schemas.openxmlformats.org/package/2006/content-types",
}
W = f"{{{NS['w']}}}"
R = f"{{{NS['r']}}}"
A = f"{{{NS['a']}}}"
PR = f"{{{NS['pr']}}}"
_PLACEHOLDER = re.compile(r"\{\{[^{}\r\n]{1,100}\}\}")


class DocxParagraphSummary(BaseModel):
    anchor: str
    text: str
    style: str | None = None
    location: Literal["body", "header", "footer", "table_cell"]


class DocxTableSummary(BaseModel):
    anchor: str
    rows: int
    columns: int


class DocxImageSummary(BaseModel):
    anchor: str
    location: Literal["body", "header", "footer", "table_cell"]
    width_emu: int | None = None
    height_emu: int | None = None


class DocxFeatureFlags(BaseModel):
    macros: bool = False
    digital_signatures: bool = False
    embedded_objects: bool = False
    active_x: bool = False
    content_controls: bool = False
    complex_fields: bool = False
    alternate_content: bool = False
    custom_xml: bool = False
    unsupported_extensions: bool = False


class DocxStructure(BaseModel):
    model_config = ConfigDict(extra="forbid")

    artifact_ref: str
    name: str
    source_id: str
    version: str
    content_hash: str
    paragraphs: list[DocxParagraphSummary]
    tables: list[DocxTableSummary]
    header_present: bool
    footer_present: bool
    section_count: int
    images: list[DocxImageSummary]
    placeholders: list[str]
    feature_flags: DocxFeatureFlags
    inspection_truncated: bool = False


class DocxRequirements(BaseModel):
    required_text: list[str] = Field(default_factory=list, max_length=50)
    required_placeholders: list[str] = Field(default_factory=list, max_length=50)
    forbidden_placeholders: list[str] = Field(default_factory=list, max_length=50)
    minimum_table_count: int = Field(default=0, ge=0, le=50)
    require_header: bool = False
    require_footer: bool = False
    expected_image_count: int | None = Field(default=None, ge=0, le=500)
    expected_section_count: int | None = Field(default=None, ge=1, le=100)


class DocxValidationResult(BaseModel):
    artifact_ref: str
    source_id: str
    version: str
    content_hash: str
    passed: bool
    checks: dict[str, bool]


class ReplaceTextOperation(BaseModel):
    operation: Literal["replace_text"]
    find: str = Field(min_length=1, max_length=500)
    replace: str = Field(max_length=MAX_REPLACEMENT_LENGTH)
    occurrence: Literal["first", "all"] = "all"
    location: Literal["body", "header", "footer", "all"] = "body"


class ReplacePlaceholderOperation(BaseModel):
    operation: Literal["replace_placeholder"]
    placeholder: str = Field(pattern=r"^\{\{[^{}\r\n]{1,100}\}\}$")
    replace: str = Field(max_length=MAX_REPLACEMENT_LENGTH)
    location: Literal["body", "header", "footer", "all"] = "all"


class SetParagraphTextOperation(BaseModel):
    operation: Literal["set_paragraph_text"]
    anchor: str
    text: str = Field(max_length=MAX_REPLACEMENT_LENGTH)


class SetTableCellOperation(BaseModel):
    operation: Literal["set_table_cell"]
    table_anchor: str
    row: int = Field(ge=0, le=500)
    column: int = Field(ge=0, le=100)
    text: str = Field(max_length=MAX_REPLACEMENT_LENGTH)


class InsertParagraphOperation(BaseModel):
    operation: Literal["insert_paragraph"]
    anchor: str
    position: Literal["before", "after"]
    text: str = Field(min_length=1, max_length=MAX_REPLACEMENT_LENGTH)


class DeleteParagraphOperation(BaseModel):
    operation: Literal["delete_paragraph"]
    anchor: str


class ReplaceImageOperation(BaseModel):
    operation: Literal["replace_image"]
    image_anchor: str
    asset_ref: str
    width_pixels: int | None = Field(default=None, ge=1, le=4096)
    height_pixels: int | None = Field(default=None, ge=1, le=4096)


class InsertImageOperation(BaseModel):
    operation: Literal["insert_image"]
    paragraph_anchor: str
    asset_ref: str
    width_pixels: int | None = Field(default=None, ge=1, le=4096)
    height_pixels: int | None = Field(default=None, ge=1, le=4096)


DocxEditOperation = Annotated[
    Union[
        ReplaceTextOperation,
        ReplacePlaceholderOperation,
        SetParagraphTextOperation,
        SetTableCellOperation,
        InsertParagraphOperation,
        DeleteParagraphOperation,
        ReplaceImageOperation,
        InsertImageOperation,
    ],
    Field(discriminator="operation"),
]


class DocxEditResult(BaseModel):
    artifact_ref: str
    source_id: str
    version: str
    content_hash: str
    applied_operations: int
    artifact_identity_preserved: bool = True
    verified: bool = True


class DocxPackage:
    """ZIP-preserving OOXML editor; untouched members remain byte-identical."""

    def __init__(self, content: bytes):
        if not content or len(content) > MAX_DOCX_BYTES:
            raise _invalid("DOCX content exceeds the bounded package size.")
        try:
            with zipfile.ZipFile(io.BytesIO(content)) as archive:
                bad = archive.testzip()
                if bad:
                    raise _invalid("The DOCX ZIP package contains a corrupt member.")
                self._members = {item.filename: archive.read(item) for item in archive.infolist()}
                self._infos = {item.filename: item for item in archive.infolist()}
        except WorkspaceAdapterError:
            raise
        except (zipfile.BadZipFile, OSError) as error:
            raise _invalid("The artifact is not a valid DOCX ZIP package.") from error
        if "[Content_Types].xml" not in self._members or "word/document.xml" not in self._members:
            raise _invalid("Required OOXML document parts are missing.")
        self._validate_paths_and_sizes()
        self.feature_flags = self._detect_features()

    @property
    def content_hash(self) -> str:
        return hashlib.sha256(self.to_bytes()).hexdigest()

    def assert_mutation_safe(self) -> None:
        flags = self.feature_flags
        unsafe = {
            "macros": flags.macros,
            "digital_signatures": flags.digital_signatures,
            "embedded_objects": flags.embedded_objects,
            "active_x": flags.active_x,
            "alternate_content": flags.alternate_content,
            "unsupported_extensions": flags.unsupported_extensions,
            "content_controls": flags.content_controls,
            "complex_fields": flags.complex_fields,
        }
        found = sorted(name for name, present in unsafe.items() if present)
        if found:
            raise WorkspaceAdapterError(
                "docx_features_unsupported",
                "DOCX mutation was refused because unsupported features are present: "
                + ", ".join(found),
                422,
            )

    def inspect(
        self,
        *,
        artifact_ref: str,
        name: str,
        source_id: str,
        artifact_id: str,
        version: str,
        codec: ArtifactReferenceCodec,
    ) -> DocxStructure:
        paragraphs: list[DocxParagraphSummary] = []
        tables: list[DocxTableSummary] = []
        images: list[DocxImageSummary] = []
        placeholders: set[str] = set()
        truncated = False
        for part, location in self._content_parts():
            root = self._xml(part)
            part_paragraphs = root.xpath(".//w:p", namespaces=NS)
            for index, paragraph in enumerate(part_paragraphs):
                text = _paragraph_text(paragraph)
                placeholders.update(_PLACEHOLDER.findall(text))
                if len(paragraphs) < MAX_INSPECTED_PARAGRAPHS:
                    paragraphs.append(
                        DocxParagraphSummary(
                            anchor=codec.encode_docx_anchor(
                                source_id=source_id,
                                artifact_id=artifact_id,
                                part=part,
                                kind="paragraph",
                                indexes=[index],
                            ),
                            text=text[:MAX_INSPECTED_TEXT],
                            style=_paragraph_style(paragraph),
                            location="table_cell" if _inside_table(paragraph) else location,
                        )
                    )
                else:
                    truncated = True
            for index, table in enumerate(root.xpath(".//w:tbl", namespaces=NS)):
                if len(tables) < MAX_INSPECTED_TABLES:
                    rows = table.xpath("./w:tr", namespaces=NS)
                    tables.append(
                        DocxTableSummary(
                            anchor=codec.encode_docx_anchor(
                                source_id=source_id,
                                artifact_id=artifact_id,
                                part=part,
                                kind="table",
                                indexes=[index],
                            ),
                            rows=len(rows),
                            columns=max(
                                (len(row.xpath("./w:tc", namespaces=NS)) for row in rows),
                                default=0,
                            ),
                        )
                    )
                else:
                    truncated = True
            for index, drawing in enumerate(root.xpath(".//w:drawing", namespaces=NS)):
                extent = drawing.xpath(".//wp:extent", namespaces=NS)
                images.append(
                    DocxImageSummary(
                        anchor=codec.encode_docx_anchor(
                            source_id=source_id,
                            artifact_id=artifact_id,
                            part=part,
                            kind="image",
                            indexes=[index],
                        ),
                        location="table_cell" if _inside_table(drawing) else location,
                        width_emu=_int_attr(extent[0], "cx") if extent else None,
                        height_emu=_int_attr(extent[0], "cy") if extent else None,
                    )
                )
        document = self._xml("word/document.xml")
        return DocxStructure(
            artifact_ref=artifact_ref,
            name=name,
            source_id=source_id,
            version=version,
            content_hash=self.content_hash,
            paragraphs=paragraphs,
            tables=tables,
            header_present=any(name.startswith("word/header") for name in self._members),
            footer_present=any(name.startswith("word/footer") for name in self._members),
            section_count=max(1, len(document.xpath(".//w:sectPr", namespaces=NS))),
            images=images,
            placeholders=sorted(placeholders)[:100],
            feature_flags=self.feature_flags,
            inspection_truncated=truncated,
        )

    def apply(
        self,
        operations: list[DocxEditOperation],
        *,
        anchor_resolver: Callable[[str], tuple[str, str, tuple[int, ...]]],
        asset_resolver: Callable[[str, int | None, int | None], AssetImage],
    ) -> None:
        self.assert_mutation_safe()
        _validate_operation_limits(operations)
        for operation in operations:
            if isinstance(operation, ReplaceTextOperation):
                count = self._replace_text(
                    operation.find,
                    operation.replace,
                    location=operation.location,
                    replace_all=operation.occurrence == "all",
                )
                if count == 0:
                    raise _target_missing("The exact replacement text was not found.")
            elif isinstance(operation, ReplacePlaceholderOperation):
                count = self._replace_text(
                    operation.placeholder,
                    operation.replace,
                    location=operation.location,
                    replace_all=True,
                )
                if count == 0:
                    raise _target_missing("The placeholder was not found.")
            elif isinstance(operation, SetParagraphTextOperation):
                paragraph = self._anchored(anchor_resolver(operation.anchor), "paragraph")
                _set_paragraph_text(paragraph, operation.text)
            elif isinstance(operation, SetTableCellOperation):
                table = self._anchored(anchor_resolver(operation.table_anchor), "table")
                rows = table.xpath("./w:tr", namespaces=NS)
                if operation.row >= len(rows):
                    raise _target_missing("The table row is outside the inspected table.")
                cells = rows[operation.row].xpath("./w:tc", namespaces=NS)
                if operation.column >= len(cells):
                    raise _target_missing("The table column is outside the inspected table.")
                paragraphs = cells[operation.column].xpath("./w:p", namespaces=NS)
                if not paragraphs:
                    paragraph = etree.SubElement(cells[operation.column], W + "p")
                else:
                    paragraph = paragraphs[0]
                _set_paragraph_text(paragraph, operation.text)
                for extra in paragraphs[1:]:
                    extra.getparent().remove(extra)
                table.store()
            elif isinstance(operation, InsertParagraphOperation):
                paragraph = self._anchored(anchor_resolver(operation.anchor), "paragraph")
                new_paragraph = _new_paragraph_like(paragraph, operation.text)
                paragraph.addprevious(new_paragraph) if operation.position == "before" else paragraph.addnext(new_paragraph)
                paragraph.store()
            elif isinstance(operation, DeleteParagraphOperation):
                paragraph = self._anchored(anchor_resolver(operation.anchor), "paragraph")
                paragraph.getparent().remove(paragraph.node)
                paragraph.store()
            elif isinstance(operation, ReplaceImageOperation):
                image = asset_resolver(operation.asset_ref, operation.width_pixels, operation.height_pixels)
                drawing = self._anchored(anchor_resolver(operation.image_anchor), "image")
                self._replace_image(drawing, image)
            elif isinstance(operation, InsertImageOperation):
                image = asset_resolver(operation.asset_ref, operation.width_pixels, operation.height_pixels)
                paragraph = self._anchored(anchor_resolver(operation.paragraph_anchor), "paragraph")
                self._insert_image(paragraph, image)

    def validate(self, requirements: DocxRequirements) -> dict[str, bool]:
        text = "\n".join(
            _paragraph_text(paragraph)
            for part, _ in self._content_parts()
            for paragraph in self._xml(part).xpath(".//w:p", namespaces=NS)
        )
        placeholders = set(_PLACEHOLDER.findall(text))
        document = self._xml("word/document.xml")
        checks = {
            "package_valid": True,
            "required_text": all(value in text for value in requirements.required_text),
            "required_placeholders": all(value in placeholders for value in requirements.required_placeholders),
            "forbidden_placeholders": all(value not in placeholders for value in requirements.forbidden_placeholders),
            "minimum_table_count": len(document.xpath(".//w:tbl", namespaces=NS)) >= requirements.minimum_table_count,
            "header_present": not requirements.require_header or any(name.startswith("word/header") for name in self._members),
            "footer_present": not requirements.require_footer or any(name.startswith("word/footer") for name in self._members),
        }
        if requirements.expected_image_count is not None:
            checks["expected_image_count"] = sum(
                len(self._xml(part).xpath(".//w:drawing", namespaces=NS))
                for part, _ in self._content_parts()
            ) == requirements.expected_image_count
        if requirements.expected_section_count is not None:
            checks["expected_section_count"] = max(
                1, len(document.xpath(".//w:sectPr", namespaces=NS))
            ) == requirements.expected_section_count
        return checks

    def to_bytes(self) -> bytes:
        output = io.BytesIO()
        with zipfile.ZipFile(output, "w") as archive:
            for name, content in self._members.items():
                info = self._infos[name]
                archive.writestr(info, content)
        return output.getvalue()

    def _replace_text(self, find: str, replace: str, *, location: str, replace_all: bool) -> int:
        count = 0
        for part, part_location in self._content_parts():
            if location != "all" and part_location != location:
                continue
            root = self._xml(part)
            changed = False
            for paragraph in root.xpath(".//w:p", namespaces=NS):
                replaced = _replace_across_runs(paragraph, find, replace, replace_all=replace_all)
                count += replaced
                changed |= replaced > 0
                if replaced and not replace_all:
                    break
            if changed:
                self._store_xml(part, root)
            if count and not replace_all:
                break
        return count

    def _anchored(self, locator: tuple[str, str, tuple[int, ...]], expected_kind: str):
        part, kind, indexes = locator
        if kind != expected_kind or len(indexes) != 1 or part not in self._members:
            raise _target_missing("The structural anchor does not match the operation.")
        root = self._xml(part)
        xpath = {"paragraph": ".//w:p", "table": ".//w:tbl", "image": ".//w:drawing"}[kind]
        nodes = root.xpath(xpath, namespaces=NS)
        if indexes[0] >= len(nodes):
            raise _target_missing("The structural anchor no longer exists.")
        return _TrackedNode(self, part, root, nodes[indexes[0]])

    def _replace_image(self, tracked, image: AssetImage) -> None:
        drawing = tracked.node
        blips = drawing.xpath(".//a:blip", namespaces=NS)
        if len(blips) != 1:
            raise _target_missing("The image relationship is ambiguous.")
        relation_id = blips[0].get(R + "embed")
        target = self._relationship_target(tracked.part, relation_id)
        expected_extension = "png" if image.mime_type == "image/png" else "jpg"
        current_extension = target.rsplit(".", 1)[-1].casefold()
        if current_extension not in ({"png"} if expected_extension == "png" else {"jpg", "jpeg"}):
            replacement = self._next_media_name(expected_extension)
            self._members[replacement] = image.content
            self._infos[replacement] = zipfile.ZipInfo(replacement)
            rels_name = _rels_name(tracked.part)
            rels = self._xml(rels_name)
            matches = rels.xpath(f"./pr:Relationship[@Id='{relation_id}']", namespaces=NS)
            matches[0].set(
                "Target", posixpath.relpath(replacement, posixpath.dirname(tracked.part))
            )
            self._store_xml(rels_name, rels)
            target = replacement
        else:
            self._members[target] = image.content
        self._update_image_content_type(target, image.mime_type)
        _set_drawing_extent(drawing, image.width_pixels, image.height_pixels)
        tracked.store()

    def _insert_image(self, tracked, image: AssetImage) -> None:
        part = tracked.part
        rels_name = _rels_name(part)
        rels = self._xml(rels_name) if rels_name in self._members else etree.Element(PR + "Relationships", nsmap={None: NS["pr"]})
        ids = {item.get("Id") for item in rels}
        number = 1
        while f"rId{number}" in ids:
            number += 1
        relation_id = f"rId{number}"
        media_name = self._next_media_name("png" if image.mime_type == "image/png" else "jpg")
        target = posixpath.relpath(media_name, posixpath.dirname(part))
        etree.SubElement(
            rels,
            PR + "Relationship",
            Id=relation_id,
            Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image",
            Target=target,
        )
        self._members[media_name] = image.content
        self._infos[media_name] = zipfile.ZipInfo(media_name)
        self._store_xml(rels_name, rels, create=True)
        self._update_image_content_type(media_name, image.mime_type)
        existing_ids = [
            int(value)
            for value in tracked.root.xpath(".//wp:docPr/@id", namespaces=NS)
            if str(value).isdigit()
        ]
        tracked.node.append(
            _drawing_run(
                relation_id,
                image.width_pixels,
                image.height_pixels,
                max(existing_ids, default=0) + 1,
            )
        )
        tracked.store()

    def _relationship_target(self, part: str, relation_id: str | None) -> str:
        if not relation_id:
            raise _target_missing("The image relationship is missing.")
        rels_name = _rels_name(part)
        if rels_name not in self._members:
            raise _target_missing("The image relationship part is missing.")
        rels = self._xml(rels_name)
        matches = rels.xpath(f"./pr:Relationship[@Id='{relation_id}']", namespaces=NS)
        if len(matches) != 1 or matches[0].get("TargetMode") == "External":
            raise _target_missing("The image relationship is missing or external.")
        target = posixpath.normpath(posixpath.join(posixpath.dirname(part), matches[0].get("Target")))
        if not target.startswith("word/") or target not in self._members:
            raise _target_missing("The image media part is unavailable.")
        return target

    def _next_media_name(self, extension: str) -> str:
        number = 1
        while f"word/media/brunova-image-{number}.{extension}" in self._members:
            number += 1
        return f"word/media/brunova-image-{number}.{extension}"

    def _update_image_content_type(self, member: str, mime_type: str) -> None:
        extension = member.rsplit(".", 1)[-1].casefold()
        root = self._xml("[Content_Types].xml")
        defaults = root.xpath(f"./ct:Default[@Extension='{extension}']", namespaces=NS)
        if defaults:
            defaults[0].set("ContentType", mime_type)
        else:
            etree.SubElement(root, f"{{{NS['ct']}}}Default", Extension=extension, ContentType=mime_type)
        self._store_xml("[Content_Types].xml", root)

    def _content_parts(self):
        yield "word/document.xml", "body"
        for name in sorted(self._members):
            if re.fullmatch(r"word/header\d*\.xml", name):
                yield name, "header"
            elif re.fullmatch(r"word/footer\d*\.xml", name):
                yield name, "footer"

    def _xml(self, name: str):
        try:
            return _safe_xml(self._members[name])
        except Exception as error:
            raise _invalid(f"OOXML part {name!r} is malformed or unsafe.") from error

    def _store_xml(self, name: str, root, *, create: bool = False) -> None:
        self._members[name] = etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)
        if create and name not in self._infos:
            self._infos[name] = zipfile.ZipInfo(name)

    def _detect_features(self) -> DocxFeatureFlags:
        names = set(self._members)
        xml = b"\n".join(value for name, value in self._members.items() if name.endswith(".xml"))
        return DocxFeatureFlags(
            macros=any(name.endswith("vbaProject.bin") for name in names),
            digital_signatures=any(name.startswith("_xmlsignatures/") for name in names),
            embedded_objects=any(name.startswith("word/embeddings/") for name in names),
            active_x=any("activeX" in name for name in names),
            content_controls=b"<w:sdt" in xml,
            complex_fields=b"<w:fldChar" in xml or b"<w:instrText" in xml,
            alternate_content=b"AlternateContent" in xml,
            custom_xml=any(name.startswith("customXml/") for name in names),
            unsupported_extensions=b"<w14:" in xml or b"<w15:" in xml or b"<w16" in xml,
        )

    def _validate_paths_and_sizes(self) -> None:
        if len(self._members) > 5000 or sum(len(value) for value in self._members.values()) > 100 * 1024 * 1024:
            raise _invalid("The expanded DOCX package exceeds safe limits.")
        for name in self._members:
            if name.startswith("/") or ".." in name.split("/"):
                raise _invalid("The DOCX package contains an unsafe member path.")


class _TrackedNode:
    def __init__(self, package: DocxPackage, part: str, root, node):
        self.package, self.part, self.root, self.node = package, part, root, node

    def __getattr__(self, name):
        return getattr(self.node, name)

    def store(self):
        self.package._store_xml(self.part, self.root)


def _validate_operation_limits(operations: list[DocxEditOperation]) -> None:
    if not 1 <= len(operations) <= MAX_DOCX_OPERATIONS:
        raise WorkspaceAdapterError(
            "docx_operations_invalid", f"Provide between 1 and {MAX_DOCX_OPERATIONS} operations.", 422
        )
    table_count = sum(isinstance(item, SetTableCellOperation) for item in operations)
    paragraph_count = sum(isinstance(item, (SetParagraphTextOperation, InsertParagraphOperation, DeleteParagraphOperation)) for item in operations)
    if table_count > MAX_TABLE_MUTATIONS or paragraph_count > MAX_PARAGRAPH_MUTATIONS:
        raise WorkspaceAdapterError(
            "docx_operations_invalid", "The request exceeds bounded table or paragraph mutations.", 422
        )
    anchored = [
        item
        for item in operations
        if isinstance(
            item,
            (
                SetParagraphTextOperation,
                SetTableCellOperation,
                InsertParagraphOperation,
                DeleteParagraphOperation,
                ReplaceImageOperation,
                InsertImageOperation,
            ),
        )
    ]
    if any(isinstance(item, (InsertParagraphOperation, DeleteParagraphOperation)) for item in anchored) and len(anchored) > 1:
        raise WorkspaceAdapterError(
            "docx_anchor_sequence_ambiguous",
            "Insert/delete paragraph must be the only anchor-based operation in a request; inspect again before the next structural mutation.",
            422,
        )


def _replace_across_runs(paragraph, find: str, replace: str, *, replace_all: bool) -> int:
    runs = paragraph.xpath(".//w:t", namespaces=NS)
    if not runs:
        return 0
    count = 0
    search_from = 0
    while True:
        texts = [node.text or "" for node in runs]
        visible = "".join(texts)
        start = visible.find(find, search_from)
        if start < 0:
            break
        end = start + len(find)
        offsets = []
        cursor = 0
        for text in texts:
            offsets.append((cursor, cursor + len(text)))
            cursor += len(text)
        first = next(i for i, (_, right) in enumerate(offsets) if start < right)
        last = next(i for i, (_, right) in enumerate(offsets) if end <= right)
        first_left, _ = offsets[first]
        last_left, _ = offsets[last]
        prefix = texts[first][: start - first_left]
        suffix = texts[last][end - last_left :]
        runs[first].text = prefix + replace + (suffix if first == last else "")
        for index in range(first + 1, last):
            runs[index].text = ""
        if last != first:
            runs[last].text = suffix
        count += 1
        if not replace_all:
            break
        search_from = start + len(replace)
    return count


def _set_paragraph_text(tracked, text: str) -> None:
    paragraph = tracked.node if isinstance(tracked, _TrackedNode) else tracked
    texts = paragraph.xpath(".//w:t", namespaces=NS)
    if texts:
        texts[0].text = text
        for node in texts[1:]:
            node.text = ""
    else:
        run = etree.SubElement(paragraph, W + "r")
        etree.SubElement(run, W + "t").text = text
    if isinstance(tracked, _TrackedNode):
        tracked.store()


def _new_paragraph_like(paragraph, text: str):
    node = paragraph.node if isinstance(paragraph, _TrackedNode) else paragraph
    new = etree.Element(W + "p")
    properties = node.find(W + "pPr")
    if properties is not None:
        new.append(deepcopy(properties))
    run = etree.SubElement(new, W + "r")
    etree.SubElement(run, W + "t").text = text
    return new


def _paragraph_text(paragraph) -> str:
    return "".join(node.text or "" for node in paragraph.xpath(".//w:t", namespaces=NS))


def _paragraph_style(paragraph) -> str | None:
    styles = paragraph.xpath("./w:pPr/w:pStyle/@w:val", namespaces=NS)
    return str(styles[0]) if styles else None


def _inside_table(node) -> bool:
    return bool(node.xpath("ancestor::w:tc", namespaces=NS))


def _rels_name(part: str) -> str:
    return posixpath.join(posixpath.dirname(part), "_rels", posixpath.basename(part) + ".rels")


def _int_attr(node, name: str) -> int | None:
    try:
        return int(node.get(name))
    except (TypeError, ValueError):
        return None


def _set_drawing_extent(drawing, width_pixels: int, height_pixels: int) -> None:
    width, height = width_pixels * 9525, height_pixels * 9525
    for extent in drawing.xpath(".//wp:extent | .//a:xfrm/a:ext", namespaces=NS):
        extent.set("cx", str(width))
        extent.set("cy", str(height))


def _drawing_run(
    relation_id: str, width_pixels: int, height_pixels: int, drawing_id: int
):
    cx, cy = width_pixels * 9525, height_pixels * 9525
    xml = f'''<w:r xmlns:w="{NS['w']}" xmlns:r="{NS['r']}" xmlns:wp="{NS['wp']}" xmlns:a="{NS['a']}" xmlns:pic="{NS['pic']}">
      <w:drawing><wp:inline><wp:extent cx="{cx}" cy="{cy}"/><wp:docPr id="{drawing_id}" name="Governed image"/>
      <a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture"><pic:pic>
      <pic:nvPicPr><pic:cNvPr id="0" name="Governed image"/><pic:cNvPicPr/></pic:nvPicPr>
      <pic:blipFill><a:blip r:embed="{relation_id}"/><a:stretch><a:fillRect/></a:stretch></pic:blipFill>
      <pic:spPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></pic:spPr>
      </pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r>'''
    return _safe_xml(xml.encode())


def _safe_xml(content: bytes):
    if b"<!DOCTYPE" in content.upper() or b"<!ENTITY" in content.upper():
        raise _invalid("OOXML DTDs and entities are not allowed.")
    parser = etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        huge_tree=False,
        remove_comments=False,
        remove_pis=False,
    )
    return etree.fromstring(content, parser=parser)


def _invalid(message: str) -> WorkspaceAdapterError:
    return WorkspaceAdapterError("docx_package_invalid", message, 422)


def _target_missing(message: str) -> WorkspaceAdapterError:
    return WorkspaceAdapterError("docx_target_invalid", message, 422)
