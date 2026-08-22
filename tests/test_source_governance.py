from datetime import timezone

import pytest

from app.adapters.google_workspace.errors import WorkspaceAdapterError
from app.source_discovery.interface import (
    CandidateSource,
    DiscoveryResult,
    candidate_identifier,
)
from app.source_governance import (
    SourceProposalReceipt,
    SourceProposalStatus,
    candidate_details,
    create_source_proposal,
)
from app.source_registry import (
    Classification,
    SourceDefinition,
    SourceRegistry,
    SourceRegistryDocument,
)


def candidate() -> CandidateSource:
    return CandidateSource(
        system="google_workspace",
        location_type="shared_drive",
        location_id="internal_drive_12345",
        name="HQ Operations",
        classification_suggestion=Classification.INTERNAL_DELIVERY,
        reasons=("new shared drive detected",),
    )


def discovery_result() -> DiscoveryResult:
    return DiscoveryResult(candidates=(candidate(),), proposals=())


def registry() -> SourceRegistry:
    source = SourceDefinition.model_validate(
        {
            "id": "career_ops",
            "name": "Career Ops",
            "system": "google_workspace",
            "location_type": "folder",
            "location_id": "registered_folder_123",
            "classification": "management_only",
            "owner": ["Management"],
            "status": "active",
        }
    )
    return SourceRegistry(SourceRegistryDocument(version=1, sources=(source,)))


def test_candidate_details_return_safe_metadata_for_existing_candidate():
    source_candidate = candidate()
    response = candidate_details(
        discovery_result(),
        candidate_identifier(source_candidate),
    )

    assert response.candidate.name == "HQ Operations"
    assert response.candidate.suggested_classification == "internal_delivery"
    assert response.candidate.confidence == "medium"
    serialized = response.model_dump_json()
    assert "internal_drive_12345" not in serialized
    assert "location_id" not in serialized
    assert "permissions" not in serialized
    assert "documents" not in serialized
    assert "content" not in serialized


def test_candidate_details_reject_unknown_candidate():
    with pytest.raises(WorkspaceAdapterError) as captured:
        candidate_details(
            discovery_result(),
            "candidate_00000000000000000000000000000000",
        )

    assert captured.value.code == "source_candidate_not_found"
    assert captured.value.status_code == 404


def test_source_proposal_is_pending_and_does_not_modify_registry():
    source_registry = registry()
    before = source_registry.sources
    source_candidate = candidate()

    proposal = create_source_proposal(
        result=discovery_result(),
        candidate_id=candidate_identifier(source_candidate),
        name="HQ Operations",
        classification=Classification.MANAGEMENT_ONLY,
        reason="Reviewed by management; human approval remains pending.",
        request_id="proposal-request-123",
    )
    receipt = SourceProposalReceipt.from_proposal(proposal)

    assert proposal.proposal_id.startswith("proposal_")
    assert proposal.status == SourceProposalStatus.PENDING_REVIEW
    assert proposal.request_id == "proposal-request-123"
    assert proposal.timestamp.tzinfo == timezone.utc
    assert receipt.model_dump()["status"] == "pending_review"
    assert source_registry.sources == before
    assert set(SourceProposalStatus) == {SourceProposalStatus.PENDING_REVIEW}


def test_source_proposal_rejects_invalid_input():
    source_candidate = candidate()

    with pytest.raises(WorkspaceAdapterError) as captured:
        create_source_proposal(
            result=discovery_result(),
            candidate_id=candidate_identifier(source_candidate),
            name=" ",
            classification=Classification.MANAGEMENT_ONLY,
            reason="reviewed",
            request_id="proposal-request-123",
        )

    assert captured.value.code == "source_proposal_invalid"
