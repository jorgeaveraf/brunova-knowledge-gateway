"""Read-only contracts for future source discovery proposals.

No external system implements these contracts in v0.6. Registry changes remain
an explicit, version-controlled human decision.
"""

from dataclasses import dataclass
from typing import Protocol

from app.source_registry import Classification


@dataclass(frozen=True)
class CandidateSource:
    system: str
    location_type: str
    location_id: str
    name: str


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


class SourceDiscovery(Protocol):
    def discover(self) -> DiscoveryResult:
        """Return candidates and proposals without modifying the registry."""
        ...
