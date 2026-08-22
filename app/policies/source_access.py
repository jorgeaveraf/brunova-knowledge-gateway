"""Fail-closed allowlist policy for Google Workspace knowledge sources."""

import re
from dataclasses import dataclass

from app.adapters.google_workspace.errors import WorkspaceAdapterError
from app.adapters.google_workspace.models import WorkspaceResource
from app.config.settings import Settings

SOURCE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{10,200}$")


@dataclass(frozen=True)
class AllowedSources:
    shared_drive_ids: tuple[str, ...]
    folder_ids: tuple[str, ...]


class SourceAccessPolicy:
    def __init__(self, settings: Settings) -> None:
        self._allowed_shared_drives = frozenset(
            settings.workspace_allowed_shared_drive_ids
        )
        self._allowed_folders = frozenset(settings.workspace_allowed_folder_ids)
        self._blocked = frozenset(settings.workspace_blocked_source_ids)
        self._validate_configured_ids()

    def allowed_sources(self) -> AllowedSources:
        shared_drives = self._allowed_shared_drives - self._blocked
        folders = self._allowed_folders - self._blocked
        if not shared_drives and not folders:
            raise WorkspaceAdapterError(
                "source_policy_unconfigured",
                "No approved Workspace knowledge sources are configured.",
                503,
            )
        return AllowedSources(
            shared_drive_ids=tuple(sorted(shared_drives)),
            folder_ids=tuple(sorted(folders)),
        )

    def authorize(self, resource: WorkspaceResource) -> None:
        self.allowed_sources()
        resource_locations = {
            resource.id,
            *resource.ancestor_ids,
        }
        if resource.drive_id:
            resource_locations.add(resource.drive_id)

        if resource_locations & self._blocked:
            raise self._not_allowed()
        if resource.drive_id in self._allowed_shared_drives:
            return
        if set(resource.ancestor_ids) & self._allowed_folders:
            return
        raise self._not_allowed()

    def is_blocked_id(self, resource_id: str) -> bool:
        return resource_id in self._blocked

    def _validate_configured_ids(self) -> None:
        all_ids = (
            self._allowed_shared_drives | self._allowed_folders | self._blocked
        )
        if any(not SOURCE_ID_PATTERN.fullmatch(value) for value in all_ids):
            raise ValueError("Workspace source allowlist contains an invalid ID")

    @staticmethod
    def _not_allowed() -> WorkspaceAdapterError:
        return WorkspaceAdapterError(
            "source_not_allowed",
            "Resource is outside approved knowledge sources.",
            403,
        )
