import pytest

from app.source_registry import (
    Classification,
    SourceRegistry,
    SourceRegistryMetadata,
    SourceType,
)


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


def test_source_metadata_excludes_internal_registry_fields(tmp_path):
    registry = SourceRegistry.load(str(write_registry(tmp_path)))

    metadata = SourceRegistryMetadata.from_definition(registry.get("career_ops"))

    assert metadata.model_dump(mode="json") == {
        "id": "career_ops",
        "name": "Career Ops",
        "system": "google_workspace",
        "classification": "management_only",
        "status": "active",
        "source_type": "knowledge_source",
        "capabilities": {
            "read": True,
            "create": False,
            "update": False,
            "move": False,
            "delete": False,
            "share": False,
            "convert": False,
        },
    }


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


def test_capability_matrix_defaults_fail_closed_for_mutations(tmp_path):
    source = SourceRegistry.load(str(write_registry(tmp_path))).get("career_ops")

    assert source.capabilities.read is True
    assert source.capabilities.create is False
    assert source.capabilities.update is False
    assert source.capabilities.move is False
    assert source.capabilities.delete is False
    assert source.capabilities.share is False
    assert source.capabilities.convert is False


def test_versioned_registry_matches_approved_source_capabilities():
    registry = SourceRegistry.load("app/config/sources.yaml")

    career_ops = registry.get("career_ops").capabilities
    template = registry.get("brunova_template").capabilities
    brunova_management = registry.get("brunova_management").capabilities
    hq_client = registry.get("hq_client").capabilities
    legacy_archive = registry.get("legacy_archive")

    assert career_ops.model_dump() == {
        "read": True,
        "create": False,
        "update": False,
        "move": False,
        "delete": False,
        "share": False,
        "convert": False,
    }
    assert all(template.model_dump().values())
    assert brunova_management.model_dump() == {
        "read": True,
        "create": True,
        "update": True,
        "move": True,
        "delete": False,
        "share": False,
        "convert": True,
    }
    assert hq_client.model_dump() == {
        "read": True,
        "create": True,
        "update": True,
        "move": True,
        "delete": True,
        "share": False,
        "convert": True,
    }
    assert legacy_archive.source_type == SourceType.ARCHIVE_DESTINATION
    assert legacy_archive.capabilities.model_dump() == {
        "read": False,
        "create": False,
        "update": False,
        "move": True,
        "delete": False,
        "share": False,
        "convert": False,
    }
