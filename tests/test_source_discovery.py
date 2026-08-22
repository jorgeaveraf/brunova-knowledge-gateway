from unittest.mock import Mock

from app.source_discovery.google_workspace import GoogleWorkspaceSourceDiscovery
from app.source_discovery.interface import CandidateSource
from app.source_registry import (
    Classification,
    SourceDefinition,
    SourceRegistry,
    SourceRegistryDocument,
)


def registry():
    source = SourceDefinition.model_validate(
        {
            "id": "career_ops",
            "name": "Career Ops",
            "system": "google_workspace",
            "location_type": "folder",
            "location_id": "registered_folder_123",
            "classification": "management_only",
            "owner": ["Jorge", "Nat"],
            "status": "active",
        }
    )
    return SourceRegistry(SourceRegistryDocument(version=1, sources=(source,)))


def test_discovery_proposes_candidates_without_mutating_registry():
    source_registry = registry()
    before = source_registry.sources
    candidate = CandidateSource(
        system="google_workspace",
        location_type="shared_drive",
        location_id="finance_drive_123",
        name="Finance",
        classification_suggestion=Classification.MANAGEMENT_ONLY,
        reasons=("new shared drive detected",),
    )
    adapter = Mock()
    adapter.discover_source_candidates.return_value = [candidate]
    discovery = GoogleWorkspaceSourceDiscovery(
        adapter,
        source_registry,
        blocked_location_ids=("blocked_drive_123",),
    )

    result = discovery.discover(limit=10)

    assert result.candidates == (candidate,)
    assert result.proposals[0].proposed_id == "finance"
    assert result.proposals[0].proposed_classification == (
        Classification.MANAGEMENT_ONLY
    )
    assert source_registry.sources == before
    adapter.discover_source_candidates.assert_called_once_with(
        excluded_location_ids=frozenset(
            {"registered_folder_123", "blocked_drive_123"}
        ),
        limit=10,
    )
