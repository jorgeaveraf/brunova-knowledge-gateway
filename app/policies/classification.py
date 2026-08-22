"""Semantic classification context, separate from source authorization."""

from dataclasses import dataclass

from app.adapters.google_workspace.errors import WorkspaceAdapterError
from app.source_registry import Classification, SourceDefinition, SourceStatus


@dataclass(frozen=True)
class SourceContext:
    source_id: str
    source_name: str
    classification: Classification


class ClassificationPolicy:
    @staticmethod
    def apply(source: SourceDefinition) -> SourceContext:
        if source.status != SourceStatus.ACTIVE:
            raise WorkspaceAdapterError(
                "source_disabled",
                "The registered knowledge source is disabled.",
                403,
            )
        return SourceContext(
            source_id=source.id,
            source_name=source.name,
            classification=source.classification,
        )
