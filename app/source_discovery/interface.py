"""Read-only contracts for future source discovery proposals.

Discovery implementations never mutate the registry. Registry changes remain
an explicit, version-controlled human decision.
"""

from dataclasses import dataclass
from hashlib import sha256
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
class SourceProposalSuggestion:
    candidate: CandidateSource
    proposed_id: str
    suggested_classification: Classification
    confidence: Literal["low", "medium", "high"]
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class DiscoveryResult:
    candidates: tuple[CandidateSource, ...]
    proposals: tuple[SourceProposalSuggestion, ...]


def candidate_identifier(candidate: CandidateSource) -> str:
    """Return a stable opaque identifier without exposing the location ID."""

    identity = "\0".join(
        (candidate.system, candidate.location_type, candidate.location_id)
    )
    digest = sha256(identity.encode("utf-8")).hexdigest()[:32]
    return f"candidate_{digest}"


def candidate_confidence(
    candidate: CandidateSource,
) -> Literal["low", "medium", "high"]:
    return "medium" if candidate.location_type == "shared_drive" else "low"


class CandidateSourceMetadata(BaseModel):
    """Non-sensitive candidate metadata safe for discovery responses."""

    candidate_id: str
    name: str
    location_type: Literal["shared_drive", "folder"]
    classification_suggestion: Classification
    reason: list[str]
    exists: bool

    @classmethod
    def from_candidate(cls, candidate: CandidateSource) -> "CandidateSourceMetadata":
        return cls(
            candidate_id=candidate_identifier(candidate),
            name=candidate.name,
            location_type=candidate.location_type,
            classification_suggestion=candidate.classification_suggestion,
            reason=list(candidate.reasons),
            exists=candidate.exists,
        )


class SourceProposalCandidateMetadata(BaseModel):
    candidate_id: str
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
    def from_proposal(
        cls,
        proposal: SourceProposalSuggestion,
    ) -> "SourceProposalMetadata":
        return cls(
            candidate=SourceProposalCandidateMetadata(
                candidate_id=candidate_identifier(proposal.candidate),
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


class CandidateDetailsMetadata(BaseModel):
    candidate_id: str
    name: str
    location_type: Literal["shared_drive", "folder"]
    suggested_classification: Classification
    confidence: Literal["low", "medium", "high"]
    reasons: list[str]

    @classmethod
    def from_candidate(cls, candidate: CandidateSource) -> "CandidateDetailsMetadata":
        return cls(
            candidate_id=candidate_identifier(candidate),
            name=candidate.name,
            location_type=candidate.location_type,
            suggested_classification=candidate.classification_suggestion,
            confidence=candidate_confidence(candidate),
            reasons=list(candidate.reasons),
        )


class CandidateDetailsResponse(BaseModel):
    candidate: CandidateDetailsMetadata


class SourceDiscovery(Protocol):
    def discover(self, *, limit: int = 25) -> DiscoveryResult:
        """Return candidates and proposals without modifying the registry."""
        ...
