"""Fail-closed access policy for registered Workspace knowledge sources."""

import re
from dataclasses import dataclass

from app.adapters.google_workspace.errors import WorkspaceAdapterError
from app.adapters.google_workspace.models import WorkspaceResource
from app.config.settings import Settings
from app.policies.classification import ClassificationPolicy, SourceContext
from app.source_registry import SourceDefinition, SourceRegistry, SourceStatus

SOURCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{10,200}$")


@dataclass(frozen=True)
class AllowedSource:
    definition: SourceDefinition
    context: SourceContext


@dataclass(frozen=True)
class AllowedSources:
    shared_drives: tuple[AllowedSource, ...]
    folders: tuple[AllowedSource, ...]


class SourceAccessPolicy:
    def __init__(self, settings: Settings, registry: SourceRegistry) -> None:
        self._registry = registry
        self._blocked = frozenset(settings.workspace_blocked_source_ids)
        self._validate_configured_ids()

    def allowed_sources(self) -> AllowedSources:
        allowed: list[AllowedSource] = []
        for source in self._registry.sources:
            if (
                source.status == SourceStatus.ACTIVE
                and source.capabilities.read
                and source.location_id not in self._blocked
            ):
                allowed.append(
                    AllowedSource(
                        definition=source,
                        context=ClassificationPolicy.apply(source),
                    )
                )
        if not allowed:
            raise WorkspaceAdapterError(
                "source_policy_unconfigured",
                "No approved Workspace knowledge sources are configured.",
                503,
            )
        return AllowedSources(
            shared_drives=tuple(
                source
                for source in allowed
                if source.definition.location_type == "shared_drive"
            ),
            folders=tuple(
                source
                for source in allowed
                if source.definition.location_type == "folder"
            ),
        )

    def authorize_source(
        self,
        source: SourceDefinition,
        *,
        require_read: bool = True,
    ) -> AllowedSource:
        """Authorize registry metadata before it is passed to an adapter."""

        try:
            registered_source = self._registry.get(source.id)
        except KeyError as error:
            raise self._not_allowed() from error
        if registered_source != source or source.location_id in self._blocked:
            raise self._not_allowed()
        if require_read and not source.capabilities.read:
            raise WorkspaceAdapterError(
                "source_capability_denied",
                "The selected source does not allow read access.",
                403,
            )
        return AllowedSource(
            definition=source,
            context=ClassificationPolicy.apply(source),
        )

    def authorize(self, resource: WorkspaceResource) -> SourceContext:
        resource_locations = {
            resource.id,
            *resource.ancestor_ids,
        }
        if resource.drive_id:
            resource_locations.add(resource.drive_id)

        if resource_locations & self._blocked:
            raise self._not_allowed()
        for source in self._registry.sources:
            if source.location_id in resource_locations:
                return ClassificationPolicy.apply(source)
        raise self._not_allowed()

    def authorize_resource_for_source(
        self,
        resource: WorkspaceResource,
        source: AllowedSource,
    ) -> SourceContext:
        """Require a resource to belong to the explicitly selected source."""

        resource_locations = {resource.id, *resource.ancestor_ids}
        if resource.drive_id:
            resource_locations.add(resource.drive_id)
        if resource_locations & self._blocked:
            raise self._not_allowed()
        if source.definition.location_id not in resource_locations:
            raise WorkspaceAdapterError(
                "resource_not_in_source",
                "The requested resource does not belong to the selected source.",
                403,
            )
        return source.context

    def is_blocked_id(self, resource_id: str) -> bool:
        return resource_id in self._blocked

    def _validate_configured_ids(self) -> None:
        if any(not SOURCE_ID_PATTERN.fullmatch(value) for value in self._blocked):
            raise ValueError("Workspace blocked source list contains an invalid ID")

    @staticmethod
    def _not_allowed() -> WorkspaceAdapterError:
        return WorkspaceAdapterError(
            "source_not_allowed",
            "Resource is outside approved knowledge sources.",
            403,
        )
