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


@dataclass(frozen=True)
class SourceProposal:
    candidate: CandidateSource
    proposed_id: str
    proposed_classification: Classification
    rationale: str


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

    @classmethod
    def from_candidate(cls, candidate: CandidateSource) -> "CandidateSourceMetadata":
        return cls(
            name=candidate.name,
            location_type=candidate.location_type,
            classification_suggestion=candidate.classification_suggestion,
            reason=list(candidate.reasons),
        )


class DiscoveryResponse(BaseModel):
    candidates: list[CandidateSourceMetadata]


class SourceDiscovery(Protocol):
    def discover(self, *, limit: int = 25) -> DiscoveryResult:
        """Return candidates and proposals without modifying the registry."""
        ...
