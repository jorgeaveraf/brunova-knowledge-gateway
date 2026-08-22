"""Governance support models that never approve or apply source changes."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from uuid import uuid4

from pydantic import BaseModel

from app.adapters.google_workspace.errors import WorkspaceAdapterError
from app.source_discovery.interface import (
    CandidateDetailsMetadata,
    CandidateDetailsResponse,
    CandidateSource,
    DiscoveryResult,
    candidate_identifier,
)
from app.source_registry import Classification

CANDIDATE_ID_PATTERN = re.compile(r"^candidate_[a-f0-9]{32}$")


class SourceProposalStatus(str, Enum):
    PENDING_REVIEW = "pending_review"


@dataclass(frozen=True)
class SourceProposal:
    """Internal immutable intent; it has no approval or apply operation."""

    proposal_id: str
    candidate: CandidateSource
    suggested_name: str
    classification: Classification
    reason: str
    status: SourceProposalStatus
    timestamp: datetime
    request_id: str


class SourceProposalReceipt(BaseModel):
    proposal_id: str
    status: SourceProposalStatus

    @classmethod
    def from_proposal(cls, proposal: SourceProposal) -> "SourceProposalReceipt":
        return cls(proposal_id=proposal.proposal_id, status=proposal.status)


def candidate_details(
    result: DiscoveryResult,
    candidate_id: str,
) -> CandidateDetailsResponse:
    candidate = _candidate_from_result(result, candidate_id)
    return CandidateDetailsResponse(
        candidate=CandidateDetailsMetadata.from_candidate(candidate)
    )


def create_source_proposal(
    *,
    result: DiscoveryResult,
    candidate_id: str,
    name: str,
    classification: Classification,
    reason: str,
    request_id: str,
) -> SourceProposal:
    candidate = _candidate_from_result(result, candidate_id)
    suggested_name = name.strip()
    rationale = reason.strip()
    if not suggested_name or len(suggested_name) > 100:
        raise WorkspaceAdapterError(
            "source_proposal_invalid",
            "Proposal name must contain between 1 and 100 characters.",
            422,
        )
    if not rationale or len(rationale) > 1000:
        raise WorkspaceAdapterError(
            "source_proposal_invalid",
            "Proposal reason must contain between 1 and 1000 characters.",
            422,
        )
    return SourceProposal(
        proposal_id=f"proposal_{uuid4().hex}",
        candidate=candidate,
        suggested_name=suggested_name,
        classification=classification,
        reason=rationale,
        status=SourceProposalStatus.PENDING_REVIEW,
        timestamp=datetime.now(timezone.utc),
        request_id=request_id,
    )


def _candidate_from_result(
    result: DiscoveryResult,
    candidate_id: str,
) -> CandidateSource:
    if CANDIDATE_ID_PATTERN.fullmatch(candidate_id):
        for candidate in result.candidates:
            if candidate_identifier(candidate) == candidate_id:
                return candidate
    raise WorkspaceAdapterError(
        "source_candidate_not_found",
        "The requested source candidate was not found.",
        404,
    )
