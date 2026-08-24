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
