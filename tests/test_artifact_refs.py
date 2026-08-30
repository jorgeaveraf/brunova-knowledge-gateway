import pytest

from app.adapters.google_workspace.errors import WorkspaceAdapterError
from app.artifact_refs import ArtifactReferenceCodec


def test_artifact_reference_is_opaque_source_bound_and_actionable():
    codec = ArtifactReferenceCodec.for_testing("ephemeral-unit-test-key")

    reference = codec.encode(source_id="career_ops", artifact_id="drive_file_12345")

    assert reference.startswith("artifact_")
    assert "drive_file_12345" not in reference
    assert codec.decode(reference, source_id="career_ops") == "drive_file_12345"


def test_artifact_reference_rejects_wrong_source_and_tampering():
    codec = ArtifactReferenceCodec.for_testing("ephemeral-unit-test-key")
    reference = codec.encode(source_id="career_ops", artifact_id="drive_file_12345")

    with pytest.raises(WorkspaceAdapterError) as wrong_source:
        codec.decode(reference, source_id="management")
    with pytest.raises(WorkspaceAdapterError) as tampered:
        codec.decode(reference[:-1] + ("A" if reference[-1] != "A" else "B"), source_id="career_ops")

    assert wrong_source.value.code == "artifact_reference_invalid"
    assert tampered.value.code == "artifact_reference_invalid"


def test_tab_reference_is_opaque_and_bound_to_source_and_artifact():
    codec = ArtifactReferenceCodec.for_testing("ephemeral-unit-test-key")

    reference = codec.encode_tab(
        source_id="career_ops", artifact_id="drive_file_12345", tab_id="google-tab-7"
    )

    assert reference.startswith("tab_")
    assert "google-tab-7" not in reference
    assert (
        codec.decode_tab(
            reference, source_id="career_ops", artifact_id="drive_file_12345"
        )
        == "google-tab-7"
    )

    with pytest.raises(WorkspaceAdapterError) as wrong_artifact:
        codec.decode_tab(
            reference, source_id="career_ops", artifact_id="another-document"
        )
    with pytest.raises(WorkspaceAdapterError) as tampered:
        codec.decode_tab(
            reference[:-1] + ("A" if reference[-1] != "A" else "B"),
            source_id="career_ops",
            artifact_id="drive_file_12345",
        )

    assert wrong_artifact.value.code == "document_tab_reference_invalid"
    assert tampered.value.code == "document_tab_reference_invalid"


def test_sheet_reference_is_opaque_and_bound_to_source_and_spreadsheet():
    codec = ArtifactReferenceCodec.for_testing("ephemeral-unit-test-key")
    reference = codec.encode_sheet(
        source_id="career_ops", artifact_id="spreadsheet_12345", sheet_id="7"
    )

    assert reference.startswith("sheet_")
    assert codec.decode_sheet(
        reference, source_id="career_ops", artifact_id="spreadsheet_12345"
    ) == "7"

    with pytest.raises(WorkspaceAdapterError) as wrong_artifact:
        codec.decode_sheet(
            reference, source_id="career_ops", artifact_id="another_spreadsheet"
        )

    assert wrong_artifact.value.code == "spreadsheet_sheet_reference_invalid"


def test_asset_reference_binds_source_artifact_and_mime():
    codec = ArtifactReferenceCodec.for_testing()
    reference = codec.encode_asset(
        source_id="career_ops", artifact_id="asset_123456", mime_type="image/svg+xml"
    )
    assert "asset_123456" not in reference
    assert codec.decode_asset(reference, source_id="career_ops") == (
        "asset_123456", "image/svg+xml"
    )
    with pytest.raises(WorkspaceAdapterError):
        codec.decode_asset(reference, source_id="other_source")


def test_docx_anchor_is_opaque_and_artifact_bound():
    codec = ArtifactReferenceCodec.for_testing()
    anchor = codec.encode_docx_anchor(
        source_id="career_ops", artifact_id="docx_123456",
        part="word/document.xml", kind="paragraph", indexes=[3]
    )
    assert "document.xml" not in anchor
    assert codec.decode_docx_anchor(
        anchor, source_id="career_ops", artifact_id="docx_123456"
    ) == ("word/document.xml", "paragraph", (3,))
    with pytest.raises(WorkspaceAdapterError):
        codec.decode_docx_anchor(
            anchor, source_id="career_ops", artifact_id="docx_other"
        )
