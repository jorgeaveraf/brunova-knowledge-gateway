"""Read-only access to Google Drive metadata."""

from collections.abc import Callable
from typing import Any

from google.auth.exceptions import GoogleAuthError
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.adapters.google_workspace.auth import build_delegated_credentials
from app.adapters.google_workspace.errors import WorkspaceAdapterError, map_google_error
from app.adapters.google_workspace.models import DriveFile, WorkspaceResource
from app.config.settings import Settings
from app.policies.source_access import SourceAccessPolicy

ServiceBuilder = Callable[..., Any]

MIME_TYPES = {
    "application/vnd.google-apps.document": "document",
    "application/vnd.google-apps.spreadsheet": "spreadsheet",
    "application/vnd.google-apps.presentation": "presentation",
    "application/vnd.google-apps.folder": "folder",
}


class GoogleWorkspaceAdapter:
    """Google Workspace read adapter using delegated runtime credentials."""

    def __init__(
        self,
        settings: Settings,
        *,
        credentials_factory: Callable[[Settings], Any] = build_delegated_credentials,
        service_builder: ServiceBuilder = build,
    ) -> None:
        self._settings = settings
        self._credentials_factory = credentials_factory
        self._service_builder = service_builder

    @property
    def delegated_user(self) -> str:
        return self._settings.workspace_delegated_user

    def _drive(self) -> Any:
        try:
            credentials = self._credentials_factory(self._settings)
            return self._service_builder(
                "drive", "v3", credentials=credentials, cache_discovery=False
            )
        except (GoogleAuthError, HttpError) as error:
            raise map_google_error(error) from error
        except OSError as error:
            raise WorkspaceAdapterError(
                "credentials_unavailable",
                "Application Default Credentials are not available to the runtime.",
                503,
            ) from error

    def check_connection(self) -> None:
        self._execute_list(page_size=1, fields="files(id)")

    def list_files(
        self, *, limit: int, source_policy: SourceAccessPolicy
    ) -> list[DriveFile]:
        sources = source_policy.allowed_sources()
        files: list[DriveFile] = []
        seen_ids: set[str] = set()

        try:
            credentials = self._credentials_factory(self._settings)
            drive = self._service_builder(
                "drive", "v3", credentials=credentials, cache_discovery=False
            )
            for drive_id in sources.shared_drive_ids:
                response = (
                    drive.files()
                    .list(
                        corpora="drive",
                        driveId=drive_id,
                        includeItemsFromAllDrives=True,
                        supportsAllDrives=True,
                        q="trashed=false",
                        pageSize=limit - len(files),
                        fields="files(id,name,mimeType)",
                        orderBy="modifiedTime desc",
                    )
                    .execute()
                )
                self._append_allowed_files(
                    response, files, seen_ids, source_policy, limit
                )
                if len(files) >= limit:
                    break

            for folder_id in sources.folder_ids:
                if len(files) >= limit:
                    break
                response = (
                    drive.files()
                    .list(
                        q=f"'{folder_id}' in parents and trashed=false",
                        spaces="drive",
                        includeItemsFromAllDrives=True,
                        supportsAllDrives=True,
                        pageSize=limit - len(files),
                        fields="files(id,name,mimeType)",
                        orderBy="modifiedTime desc",
                    )
                    .execute()
                )
                self._append_allowed_files(
                    response, files, seen_ids, source_policy, limit
                )
            return files
        except (GoogleAuthError, HttpError) as error:
            raise map_google_error(error) from error

    def get_resource(self, resource_id: str) -> WorkspaceResource:
        try:
            drive = self._drive()
            metadata = self._get_file_metadata(drive, resource_id)
            ancestors: list[str] = []
            pending = list(metadata.get("parents", []))
            visited: set[str] = set()
            depth = 0
            while pending and depth < self._settings.workspace_source_max_depth:
                parent_id = pending.pop(0)
                if parent_id in visited:
                    continue
                visited.add(parent_id)
                ancestors.append(parent_id)
                parent = self._get_file_metadata(drive, parent_id)
                pending.extend(parent.get("parents", []))
                depth += 1
            return WorkspaceResource(
                id=metadata["id"],
                name=metadata.get("name", ""),
                mime_type=metadata.get("mimeType", ""),
                modified_time=metadata.get("modifiedTime", ""),
                drive_id=metadata.get("driveId"),
                ancestor_ids=tuple(ancestors),
            )
        except (GoogleAuthError, HttpError) as error:
            raise map_google_error(error) from error

    @staticmethod
    def _get_file_metadata(drive: Any, file_id: str) -> dict[str, Any]:
        return (
            drive.files()
            .get(
                fileId=file_id,
                fields="id,name,mimeType,modifiedTime,driveId,parents",
                supportsAllDrives=True,
            )
            .execute()
        )

    @staticmethod
    def _append_allowed_files(
        response: dict[str, Any],
        files: list[DriveFile],
        seen_ids: set[str],
        source_policy: SourceAccessPolicy,
        limit: int,
    ) -> None:
        for item in response.get("files", []):
            resource_id = item.get("id", "")
            if (
                not resource_id
                or resource_id in seen_ids
                or source_policy.is_blocked_id(resource_id)
            ):
                continue
            seen_ids.add(resource_id)
            files.append(
                DriveFile(
                    id=resource_id,
                    name=item.get("name", ""),
                    type=MIME_TYPES.get(item.get("mimeType"), "file"),
                )
            )
            if len(files) >= limit:
                return

    def _execute_list(self, *, page_size: int, fields: str) -> dict[str, Any]:
        try:
            return (
                self._drive()
                .files()
                .list(
                    pageSize=page_size,
                    fields=fields,
                    orderBy="modifiedTime desc",
                )
                .execute()
            )
        except (GoogleAuthError, HttpError) as error:
            raise map_google_error(error) from error
