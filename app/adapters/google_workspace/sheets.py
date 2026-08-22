"""Controlled, read-only Google Sheets range retrieval."""

from collections.abc import Callable
from typing import Any

from google.auth.exceptions import GoogleAuthError
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.adapters.google_workspace.auth import build_delegated_credentials
from app.adapters.google_workspace.errors import WorkspaceAdapterError, map_google_error
from app.adapters.google_workspace.models import SheetRangeContent, WorkspaceResource
from app.config.settings import Settings

GOOGLE_SHEET_MIME_TYPE = "application/vnd.google-apps.spreadsheet"


class GoogleSheetsAdapter:
    def __init__(
        self,
        settings: Settings,
        *,
        credentials_factory: Callable[[Settings], Any] = build_delegated_credentials,
        service_builder: Callable[..., Any] = build,
    ) -> None:
        self._settings = settings
        self._credentials_factory = credentials_factory
        self._service_builder = service_builder

    @property
    def max_cells(self) -> int:
        return self._settings.workspace_sheet_max_cells

    def get_range(
        self, resource: WorkspaceResource, *, range_name: str
    ) -> SheetRangeContent:
        try:
            if resource.mime_type != GOOGLE_SHEET_MIME_TYPE:
                raise WorkspaceAdapterError(
                    "resource_type_invalid",
                    "The requested resource is not a native Google Sheet.",
                    422,
                )
            credentials = self._credentials_factory(self._settings)
            sheets = self._service_builder(
                "sheets", "v4", credentials=credentials, cache_discovery=False
            )
            response = (
                sheets.spreadsheets()
                .values()
                .get(spreadsheetId=resource.id, range=range_name)
                .execute()
            )
            return SheetRangeContent(
                spreadsheet_id=resource.id,
                range=range_name,
                values=response.get("values", []),
            )
        except WorkspaceAdapterError:
            raise
        except (GoogleAuthError, HttpError) as error:
            raise map_google_error(error) from error
        except OSError as error:
            raise WorkspaceAdapterError(
                "credentials_unavailable",
                "Application Default Credentials are not available to the runtime.",
                503,
            ) from error
