"""Read-only access to Google Drive metadata."""

from collections.abc import Callable
from typing import Any

from google.auth.exceptions import GoogleAuthError
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.adapters.google_workspace.auth import build_delegated_credentials
from app.adapters.google_workspace.errors import WorkspaceAdapterError, map_google_error
from app.adapters.google_workspace.models import DriveFile
from app.config.settings import Settings

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

    def list_files(self, *, limit: int) -> list[DriveFile]:
        response = self._execute_list(
            page_size=limit,
            fields="files(id,name,mimeType)",
        )
        return [
            DriveFile(
                id=item.get("id", ""),
                name=item.get("name", ""),
                type=MIME_TYPES.get(item.get("mimeType"), "file"),
            )
            for item in response.get("files", [])
        ]

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
