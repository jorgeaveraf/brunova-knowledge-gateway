import io
import zipfile

from PIL import Image

from app.adapters.google_workspace.errors import WorkspaceAdapterError
from app.artifact_refs import ArtifactReferenceCodec
from app.docx_production import (
    DeleteParagraphOperation,
    DocxPackage,
    DocxRequirements,
    InsertImageOperation,
    InsertParagraphOperation,
    ReplaceImageOperation,
    ReplacePlaceholderOperation,
    ReplaceTextOperation,
    SetParagraphTextOperation,
    SetTableCellOperation,
)
from app.visual_assets import AssetImage

CONTENT_TYPES = b'''<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Default Extension="png" ContentType="image/png"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/header1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.header+xml"/>
<Override PartName="/word/footer1.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.footer+xml"/>
</Types>'''
DOCUMENT = b'''<?xml version="1.0" encoding="UTF-8"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
 xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
 xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing"
 xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><w:body>
<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>Hello {{NA</w:t></w:r><w:r><w:rPr><w:b/></w:rPr><w:t>ME}}</w:t></w:r></w:p>
<w:tbl><w:tr><w:tc><w:p><w:r><w:t>Old cell</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
<w:p><w:r><w:drawing><wp:inline><wp:extent cx="95250" cy="95250"/><wp:docPr id="1" name="Logo"/><a:graphic><a:graphicData><a:blip r:embed="rIdLogo"/></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>
<w:p><w:r><w:drawing><wp:inline><wp:extent cx="95250" cy="95250"/><wp:docPr id="2" name="Other"/><a:graphic><a:graphicData><a:blip r:embed="rIdOther"/></a:graphicData></a:graphic></wp:inline></w:drawing></w:r></w:p>
<w:p><w:pPr><w:sectPr/></w:pPr><w:r><w:t>Tail</w:t></w:r></w:p><w:sectPr><w:pgSz w:w="12240" w:h="15840"/></w:sectPr></w:body></w:document>'''
HEADER = b'''<w:hdr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:p><w:r><w:t>Header {{NAME}}</w:t></w:r></w:p></w:hdr>'''
FOOTER = b'''<w:ftr xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:p><w:r><w:t>Footer</w:t></w:r></w:p></w:ftr>'''
DOCUMENT_RELS = b'''<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rIdLogo" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/logo.png"/><Relationship Id="rIdOther" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/other.png"/></Relationships>'''


def image_bytes(color="red"):
    output = io.BytesIO()
    Image.new("RGB", (4, 4), color).save(output, format="PNG")
    return output.getvalue()


def fixture_docx():
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", CONTENT_TYPES)
        archive.writestr("word/document.xml", DOCUMENT)
        archive.writestr("word/header1.xml", HEADER)
        archive.writestr("word/footer1.xml", FOOTER)
        archive.writestr("word/_rels/document.xml.rels", DOCUMENT_RELS)
        archive.writestr("word/media/logo.png", image_bytes())
        archive.writestr("word/media/other.png", image_bytes("green"))
        archive.writestr("docProps/custom.xml", b"<Properties>preserve-me</Properties>")
    return output.getvalue()


def test_inspect_and_run_aware_mutation_preserve_untouched_parts():
    codec = ArtifactReferenceCodec.for_testing()
    package = DocxPackage(fixture_docx())
    before_custom = package._members["docProps/custom.xml"]
    structure = package.inspect(
        artifact_ref="artifact_x",
        name="Fixture.docx",
        source_id="test",
        artifact_id="docx_123456",
        version="7",
        codec=codec,
    )
    assert structure.header_present and structure.footer_present
    assert structure.section_count == 2
    assert len(structure.images) == 2
    assert structure.placeholders == ["{{NAME}}"]
    body_heading = next(item for item in structure.paragraphs if item.style == "Heading1")
    table = structure.tables[0]
    image = structure.images[0]

    package.apply(
        [
            ReplacePlaceholderOperation(
                operation="replace_placeholder", placeholder="{{NAME}}", replace="Brunova"
            ),
            SetTableCellOperation(
                operation="set_table_cell", table_anchor=table.anchor, row=0, column=0, text="New cell"
            ),
            ReplaceImageOperation(
                operation="replace_image", image_anchor=image.anchor,
                asset_ref="asset_test", width_pixels=4, height_pixels=4,
            ),
        ],
        anchor_resolver=lambda value: codec.decode_docx_anchor(
            value, source_id="test", artifact_id="docx_123456"
        ),
        asset_resolver=lambda *_: AssetImage(
            content=image_bytes("blue"), mime_type="image/png",
            width_pixels=4, height_pixels=4,
        ),
    )
    package.apply(
        [InsertParagraphOperation(
            operation="insert_paragraph", anchor=body_heading.anchor,
            position="after", text="Inserted"
        )],
        anchor_resolver=lambda value: codec.decode_docx_anchor(
            value, source_id="test", artifact_id="docx_123456"
        ),
        asset_resolver=lambda *_: None,
    )
    readback = DocxPackage(package.to_bytes())
    checks = readback.validate(
        DocxRequirements(
            required_text=["Hello Brunova", "New cell", "Inserted"],
            forbidden_placeholders=["{{NAME}}"],
            minimum_table_count=1,
            require_header=True,
            require_footer=True,
            expected_section_count=2,
            expected_image_count=2,
        )
    )
    assert all(checks.values())
    assert readback._members["docProps/custom.xml"] == before_custom
    assert readback._members["word/media/logo.png"] == image_bytes("blue")
    assert readback._members["word/media/other.png"] == image_bytes("green")
    readback_structure = readback.inspect(
        artifact_ref="artifact_x", name="Fixture.docx", source_id="test",
        artifact_id="docx_123456", version="8", codec=codec,
    )
    assert any(item.style == "Heading1" for item in readback_structure.paragraphs)
    document = readback._xml("word/document.xml")
    assert document.xpath(".//w:pgSz[@w:w='12240'][@w:h='15840']", namespaces={"w": "http://schemas.openxmlformats.org/wordprocessingml/2006/main"})


def test_feature_detection_refuses_embedded_objects():
    content = fixture_docx()
    source = io.BytesIO(content)
    output = io.BytesIO()
    with zipfile.ZipFile(source) as incoming, zipfile.ZipFile(output, "w") as archive:
        for item in incoming.infolist():
            archive.writestr(item, incoming.read(item))
        archive.writestr("word/embeddings/object1.bin", b"opaque")
    package = DocxPackage(output.getvalue())
    assert package.feature_flags.embedded_objects is True
    try:
        package.assert_mutation_safe()
    except WorkspaceAdapterError as error:
        assert "embedded_objects" in str(error)
    else:
        raise AssertionError("unsafe package was accepted")


def test_replace_all_does_not_reprocess_text_inserted_by_replacement():
    package = DocxPackage(fixture_docx())
    package.apply(
        [ReplaceTextOperation(operation="replace_text", find="l", replace="ll")],
        anchor_resolver=lambda _: ("", "", ()),
        asset_resolver=lambda *_: None,
    )
    checks = DocxPackage(package.to_bytes()).validate(
        DocxRequirements(required_text=["Hellllo"])
    )
    assert checks["required_text"] is True


def test_anchor_mutations_set_delete_and_insert_image():
    codec = ArtifactReferenceCodec.for_testing()
    package = DocxPackage(fixture_docx())
    structure = package.inspect(
        artifact_ref="artifact_x", name="Fixture.docx", source_id="test",
        artifact_id="docx_123456", version="7", codec=codec,
    )
    tail = next(item for item in structure.paragraphs if item.text == "Tail")
    footer = next(item for item in structure.paragraphs if item.text == "Footer")
    resolver = lambda value: codec.decode_docx_anchor(
            value, source_id="test", artifact_id="docx_123456"
        )
    asset_resolver = lambda *_: AssetImage(
            content=image_bytes("blue"), mime_type="image/png",
            width_pixels=4, height_pixels=4,
        )
    package.apply(
        [SetParagraphTextOperation(
            operation="set_paragraph_text", anchor=tail.anchor, text="Updated tail"
        )],
        anchor_resolver=resolver, asset_resolver=asset_resolver,
    )
    package.apply(
        [DeleteParagraphOperation(operation="delete_paragraph", anchor=footer.anchor)],
        anchor_resolver=resolver, asset_resolver=asset_resolver,
    )
    package.apply(
        [InsertImageOperation(
            operation="insert_image", paragraph_anchor=tail.anchor,
            asset_ref="asset_test", width_pixels=4, height_pixels=4,
        )],
        anchor_resolver=resolver, asset_resolver=asset_resolver,
    )
    checks = DocxPackage(package.to_bytes()).validate(
        DocxRequirements(
            required_text=["Updated tail"], expected_image_count=3,
            require_footer=True,
        )
    )
    assert all(checks.values())
