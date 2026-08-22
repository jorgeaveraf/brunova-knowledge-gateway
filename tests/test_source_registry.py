import pytest

from app.source_registry import Classification, SourceRegistry


def write_registry(tmp_path, *, classification="management_only", status="active"):
    path = tmp_path / "sources.yaml"
    path.write_text(
        f"""version: 1
sources:
  - id: career_ops
    name: Career Ops
    system: google_workspace
    location_type: folder
    location_id: folder_123456789
    classification: {classification}
    owner: [Jorge, Nat]
    status: {status}
""",
        encoding="utf-8",
    )
    return path


def test_existing_source_is_loaded_and_missing_source_raises(tmp_path):
    registry = SourceRegistry.load(str(write_registry(tmp_path)))

    assert registry.get("career_ops").name == "Career Ops"
    with pytest.raises(KeyError):
        registry.get("missing_source")


@pytest.mark.parametrize(
    "classification",
    [
        "management_only",
        "internal_delivery",
        "client_shareable",
        "public",
    ],
)
def test_defined_classifications_are_valid(tmp_path, classification):
    registry = SourceRegistry.load(
        str(write_registry(tmp_path, classification=classification))
    )

    assert registry.get("career_ops").classification == Classification(classification)


def test_undefined_classification_is_rejected(tmp_path):
    with pytest.raises(ValueError, match="Invalid source registry"):
        SourceRegistry.load(
            str(write_registry(tmp_path, classification="confidential"))
        )
