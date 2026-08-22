from datetime import datetime, timezone

import pytest

from app.adapters.google_workspace.errors import WorkspaceAdapterError
from app.source_discovery.interface import CandidateSource
from app.source_governance import SourceProposal, SourceProposalStatus
from app.source_proposal_store import (
    ProposalObjectConflict,
    YamlSourceProposalStore,
)
from app.source_registry import Classification


class MemoryObjectBackend:
    def __init__(self):
        self.content = None
        self.generation = 0
        self.conflicts_remaining = 0

    def read(self):
        return self.content, self.generation

    def write(self, content, *, generation):
        if self.conflicts_remaining:
            self.conflicts_remaining -= 1
            raise ProposalObjectConflict
        if generation != self.generation:
            raise ProposalObjectConflict
        self.content = content
        self.generation += 1


def proposal() -> SourceProposal:
    return SourceProposal(
        proposal_id="proposal_1234567890abcdef1234567890abcdef",
        candidate=CandidateSource(
            system="google_workspace",
            location_type="shared_drive",
            location_id="internal_drive_12345",
            name="HQ Operations",
            classification_suggestion=Classification.INTERNAL_DELIVERY,
            reasons=("new shared drive detected",),
        ),
        suggested_name="HQ Operations",
        classification=Classification.MANAGEMENT_ONLY,
        reason="Management review requested.",
        status=SourceProposalStatus.PENDING_REVIEW,
        timestamp=datetime(2026, 8, 22, 12, 0, tzinfo=timezone.utc),
        request_id="proposal-request-123",
    )


def test_proposal_is_created_and_recoverable_after_store_restart():
    backend = MemoryObjectBackend()
    first_instance = YamlSourceProposalStore(backend)

    created = first_instance.create(proposal())
    restarted_instance = YamlSourceProposalStore(backend)
    recovered = restarted_instance.get(created.proposal_id)

    assert restarted_instance.list() == (recovered,)
    assert recovered.candidate_name == "HQ Operations"
    assert recovered.status == "pending_review"
    assert recovered.request_id == "proposal-request-123"
    assert backend.generation == 1


def test_persisted_yaml_excludes_internal_workspace_data():
    backend = MemoryObjectBackend()
    YamlSourceProposalStore(backend).create(proposal())

    assert "internal_drive_12345" not in backend.content
    assert "location_id" not in backend.content
    assert "permissions" not in backend.content
    assert "users" not in backend.content
    assert "content" not in backend.content
    assert "approved" not in backend.content
    assert "applied" not in backend.content


def test_store_retries_conditional_write_conflicts():
    backend = MemoryObjectBackend()
    backend.conflicts_remaining = 1

    record = YamlSourceProposalStore(backend).create(proposal())

    assert record.proposal_id == proposal().proposal_id
    assert backend.generation == 1


def test_store_rejects_missing_proposal():
    with pytest.raises(WorkspaceAdapterError) as captured:
        YamlSourceProposalStore(MemoryObjectBackend()).get(
            "proposal_00000000000000000000000000000000"
        )

    assert captured.value.code == "source_proposal_not_found"
