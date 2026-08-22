"""Google Workspace discovery that proposes sources without registry mutation."""

import re

from app.adapters.google_workspace.drive import GoogleWorkspaceAdapter
from app.source_discovery.interface import (
    DiscoveryResult,
    SourceProposal,
)
from app.source_registry import SourceRegistry


class GoogleWorkspaceSourceDiscovery:
    def __init__(
        self,
        adapter: GoogleWorkspaceAdapter,
        registry: SourceRegistry,
        *,
        blocked_location_ids: tuple[str, ...] = (),
    ) -> None:
        self._adapter = adapter
        self._registry = registry
        self._blocked_location_ids = frozenset(blocked_location_ids)

    def discover(self, *, limit: int = 25) -> DiscoveryResult:
        excluded = {
            *(source.location_id for source in self._registry.sources),
            *self._blocked_location_ids,
        }
        candidates = tuple(
            self._adapter.discover_source_candidates(
                excluded_location_ids=frozenset(excluded),
                limit=limit,
            )
        )
        proposals = tuple(
            SourceProposal(
                candidate=candidate,
                proposed_id=_proposed_id(candidate.name),
                suggested_classification=candidate.classification_suggestion,
                confidence=(
                    "medium" if candidate.location_type == "shared_drive" else "low"
                ),
                reasons=candidate.reasons,
            )
            for candidate in candidates
        )
        return DiscoveryResult(candidates=candidates, proposals=proposals)


def _proposed_id(name: str) -> str:
    normalized = re.sub(r"[^a-z0-9]+", "_", name.strip().lower()).strip("_")
    if not normalized or not normalized[0].isalpha():
        normalized = f"source_{normalized}".rstrip("_")
    return normalized[:64]
