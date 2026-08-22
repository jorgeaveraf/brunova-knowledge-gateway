from dataclasses import replace
from unittest.mock import Mock

from app.source_discovery.google_workspace import GoogleWorkspaceSourceDiscovery
from app.source_discovery.interface import CandidateSource, candidate_identifier
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
    assert result.proposals[0].suggested_classification == (
        Classification.MANAGEMENT_ONLY
    )
    assert result.proposals[0].confidence == "medium"
    assert result.proposals[0].reasons == ("new shared drive detected",)
    opaque_id = candidate_identifier(candidate)
    assert opaque_id.startswith("candidate_")
    assert candidate.location_id not in opaque_id
    assert opaque_id == candidate_identifier(replace(candidate, name="Finance Team"))
    assert opaque_id != candidate_identifier(
        replace(candidate, location_id="another_drive_123")
    )
    assert source_registry.sources == before
    adapter.discover_source_candidates.assert_called_once_with(
        excluded_location_ids=frozenset(
            {"registered_folder_123", "blocked_drive_123"}
        ),
        limit=10,
    )


def test_root_folder_proposal_uses_conservative_confidence():
    candidate = CandidateSource(
        system="google_workspace",
        location_type="folder",
        location_id="root_folder_12345",
        name="00 Brunova HQ",
        classification_suggestion=Classification.MANAGEMENT_ONLY,
        reasons=("unregistered root folder detected",),
    )
    adapter = Mock()
    adapter.discover_source_candidates.return_value = [candidate]

    result = GoogleWorkspaceSourceDiscovery(adapter, registry()).discover(limit=5)

    assert result.proposals[0].confidence == "low"
    assert result.proposals[0].suggested_classification == "management_only"
