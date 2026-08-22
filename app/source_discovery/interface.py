"""Read-only contracts for future source discovery proposals.

Discovery implementations never mutate the registry. Registry changes remain
an explicit, version-controlled human decision.
"""

from dataclasses import dataclass
from typing import Literal, Protocol

from pydantic import BaseModel

from app.source_registry import Classification


@dataclass(frozen=True)
class CandidateSource:
    system: str
    location_type: str
    location_id: str
    name: str
    classification_suggestion: Classification
    reasons: tuple[str, ...]
    exists: bool = True


@dataclass(frozen=True)
class SourceProposal:
    candidate: CandidateSource
    proposed_id: str
    suggested_classification: Classification
    confidence: Literal["low", "medium", "high"]
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class DiscoveryResult:
    candidates: tuple[CandidateSource, ...]
    proposals: tuple[SourceProposal, ...]


class CandidateSourceMetadata(BaseModel):
    """Non-sensitive candidate metadata safe for discovery responses."""

    name: str
    location_type: Literal["shared_drive", "folder"]
    classification_suggestion: Classification
    reason: list[str]
    exists: bool

    @classmethod
    def from_candidate(cls, candidate: CandidateSource) -> "CandidateSourceMetadata":
        return cls(
            name=candidate.name,
            location_type=candidate.location_type,
            classification_suggestion=candidate.classification_suggestion,
            reason=list(candidate.reasons),
            exists=candidate.exists,
        )


class SourceProposalCandidateMetadata(BaseModel):
    name: str
    location_type: Literal["shared_drive", "folder"]


class SourceProposalMetadata(BaseModel):
    """Executable-free proposal safe for agent comparison and human review."""

    type: Literal["new_source_proposal"] = "new_source_proposal"
    candidate: SourceProposalCandidateMetadata
    suggested_classification: Classification
    confidence: Literal["low", "medium", "high"]
    reason: list[str]

    @classmethod
    def from_proposal(cls, proposal: SourceProposal) -> "SourceProposalMetadata":
        return cls(
            candidate=SourceProposalCandidateMetadata(
                name=proposal.candidate.name,
                location_type=proposal.candidate.location_type,
            ),
            suggested_classification=proposal.suggested_classification,
            confidence=proposal.confidence,
            reason=list(proposal.reasons),
        )


class DiscoveryResponse(BaseModel):
    candidates: list[CandidateSourceMetadata]
    proposals: list[SourceProposalMetadata]

    @classmethod
    def from_result(cls, result: DiscoveryResult) -> "DiscoveryResponse":
        return cls(
            candidates=[
                CandidateSourceMetadata.from_candidate(candidate)
                for candidate in result.candidates
            ],
            proposals=[
                SourceProposalMetadata.from_proposal(proposal)
                for proposal in result.proposals
            ],
        )


class SourceDiscovery(Protocol):
    def discover(self, *, limit: int = 25) -> DiscoveryResult:
        """Return candidates and proposals without modifying the registry."""
        ...
