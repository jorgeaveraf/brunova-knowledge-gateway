"""Controlled Google Sheets retrieval and allowlisted semantic mutations."""

from collections.abc import Callable
from typing import Any

from google.auth.exceptions import GoogleAuthError
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.adapters.google_workspace.auth import build_delegated_credentials
from app.adapters.google_workspace.errors import WorkspaceAdapterError, map_google_error
from app.adapters.google_workspace.models import SheetRangeContent, WorkspaceResource
from app.config.settings import Settings
from app.policies.workspace import SpreadsheetMutationPolicy
from app.spreadsheet_production import SpreadsheetEditOperation

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
        self,
        resource: WorkspaceResource,
        *,
        range_name: str,
        value_render_option: str = "FORMATTED_VALUE",
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
            values = sheets.spreadsheets().values()
            parameters = {"spreadsheetId": resource.id, "range": range_name}
            if value_render_option != "FORMATTED_VALUE":
                parameters["valueRenderOption"] = value_render_option
            response = values.get(**parameters).execute()
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

    def get_structure(self, resource: WorkspaceResource) -> dict[str, Any]:
        """Return only allowlisted spreadsheet and grid metadata."""

        self._validate_native_sheet(resource)
        try:
            response = (
                self._sheets()
                .spreadsheets()
                .get(
                    spreadsheetId=resource.id,
                    includeGridData=False,
                    fields=(
                        "properties(title,locale,timeZone),"
                        "sheets(properties(sheetId,title,index,gridProperties("
                        "rowCount,columnCount,frozenRowCount,frozenColumnCount)))"
                    ),
                )
                .execute()
            )
            return {
                "title": response.get("properties", {}).get("title", resource.name),
                "locale": response.get("properties", {}).get("locale"),
                "time_zone": response.get("properties", {}).get("timeZone"),
                "sheets": [
                    {
                        "sheet_id": str(item.get("properties", {}).get("sheetId", "")),
                        "title": item.get("properties", {}).get("title", ""),
                        "index": int(item.get("properties", {}).get("index", 0)),
                        "row_count": int(
                            item.get("properties", {})
                            .get("gridProperties", {})
                            .get("rowCount", 0)
                        ),
                        "column_count": int(
                            item.get("properties", {})
                            .get("gridProperties", {})
                            .get("columnCount", 0)
                        ),
                        "frozen_row_count": int(
                            item.get("properties", {})
                            .get("gridProperties", {})
                            .get("frozenRowCount", 0)
                        ),
                        "frozen_column_count": int(
                            item.get("properties", {})
                            .get("gridProperties", {})
                            .get("frozenColumnCount", 0)
                        ),
                    }
                    for item in response.get("sheets", [])
                ],
            }
        except (GoogleAuthError, HttpError) as error:
            raise map_google_error(error) from error
        except OSError as error:
            raise WorkspaceAdapterError(
                "credentials_unavailable",
                "Application Default Credentials are not available to the runtime.",
                503,
            ) from error

    def apply_operations(
        self,
        resource: WorkspaceResource,
        *,
        operations: list[SpreadsheetEditOperation],
        sheet_id_resolver: Callable[[str], int],
        sheet_title_resolver: Callable[[str], str],
    ) -> None:
        """Execute only modeled operations; raw Sheets requests are never accepted."""

        self._validate_native_sheet(resource)
        try:
            sheets = self._sheets()
            pending_requests: list[dict[str, Any]] = []

            def flush_structural_requests() -> None:
                if not pending_requests:
                    return
                (
                    sheets.spreadsheets()
                    .batchUpdate(
                        spreadsheetId=resource.id,
                        body={"requests": list(pending_requests)},
                    )
                    .execute()
                )
                pending_requests.clear()

            for operation in operations:
                if operation.operation == "set_values":
                    flush_structural_requests()
                    (
                        sheets.spreadsheets()
                        .values()
                        .update(
                            spreadsheetId=resource.id,
                            range=_qualified_range(
                                sheet_title_resolver(operation.sheet_ref),
                                operation.range,
                            ),
                            valueInputOption=operation.value_input_option,
                            body={"values": operation.values},
                        )
                        .execute()
                    )
                elif operation.operation == "append_rows":
                    flush_structural_requests()
                    (
                        sheets.spreadsheets()
                        .values()
                        .append(
                            spreadsheetId=resource.id,
                            range=_qualified_range(
                                sheet_title_resolver(operation.sheet_ref),
                                operation.range,
                            ),
                            valueInputOption=operation.value_input_option,
                            insertDataOption="INSERT_ROWS",
                            body={"values": operation.values},
                        )
                        .execute()
                    )
                elif operation.operation == "clear_range":
                    flush_structural_requests()
                    (
                        sheets.spreadsheets()
                        .values()
                        .clear(
                            spreadsheetId=resource.id,
                            range=_qualified_range(
                                sheet_title_resolver(operation.sheet_ref),
                                operation.range,
                            ),
                            body={},
                        )
                        .execute()
                    )
                else:
                    pending_requests.append(
                        _structural_request(
                            operation,
                            sheet_id_resolver=sheet_id_resolver,
                            max_cells=self.max_cells,
                        )
                    )
            flush_structural_requests()
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

    def _sheets(self) -> Any:
        credentials = self._credentials_factory(self._settings)
        return self._service_builder(
            "sheets", "v4", credentials=credentials, cache_discovery=False
        )

    @staticmethod
    def _validate_native_sheet(resource: WorkspaceResource) -> None:
        if resource.mime_type != GOOGLE_SHEET_MIME_TYPE:
            raise WorkspaceAdapterError(
                "resource_type_invalid",
                "The requested resource is not a native Google Sheet.",
                422,
            )


def _structural_request(
    operation: SpreadsheetEditOperation,
    *,
    sheet_id_resolver: Callable[[str], int],
    max_cells: int,
) -> dict[str, Any]:
    if operation.operation == "create_sheet":
        return {
            "addSheet": {
                "properties": {
                    "title": operation.title,
                    "gridProperties": {
                        "rowCount": operation.row_count,
                        "columnCount": operation.column_count,
                    },
                }
            }
        }
    if operation.operation == "rename_sheet":
        return {
            "updateSheetProperties": {
                "properties": {
                    "sheetId": sheet_id_resolver(operation.sheet_ref),
                    "title": operation.title,
                },
                "fields": "title",
            }
        }
    if operation.operation == "delete_sheet":
        return {"deleteSheet": {"sheetId": sheet_id_resolver(operation.sheet_ref)}}
    if operation.operation in {
        "insert_rows", "delete_rows", "insert_columns", "delete_columns"
    }:
        dimension = "ROWS" if operation.operation.endswith("rows") else "COLUMNS"
        dimension_range = {
            "sheetId": sheet_id_resolver(operation.sheet_ref),
            "dimension": dimension,
            "startIndex": operation.start_index,
            "endIndex": operation.start_index + operation.count,
        }
        if operation.operation.startswith("insert"):
            return {
                "insertDimension": {
                    "range": dimension_range,
                    "inheritFromBefore": False,
                }
            }
        return {"deleteDimension": {"range": dimension_range}}
    if operation.operation == "format_range":
        parsed = SpreadsheetMutationPolicy.parse_range(
            operation.range, max_cells=max_cells
        )
        sheet_id = sheet_id_resolver(operation.sheet_ref)
        cell: dict[str, Any] = {}
        fields: list[str] = []
        text_format: dict[str, Any] = {}
        if operation.bold is not None:
            text_format["bold"] = operation.bold
            fields.append("userEnteredFormat.textFormat.bold")
        if operation.italic is not None:
            text_format["italic"] = operation.italic
            fields.append("userEnteredFormat.textFormat.italic")
        if operation.font_size is not None:
            text_format["fontSize"] = operation.font_size
            fields.append("userEnteredFormat.textFormat.fontSize")
        if operation.text_color is not None:
            text_format["foregroundColor"] = _rgb(operation.text_color)
            fields.append("userEnteredFormat.textFormat.foregroundColor")
        if text_format:
            cell.setdefault("userEnteredFormat", {})["textFormat"] = text_format
        if operation.horizontal_alignment is not None:
            cell.setdefault("userEnteredFormat", {})[
                "horizontalAlignment"
            ] = operation.horizontal_alignment
            fields.append("userEnteredFormat.horizontalAlignment")
        if operation.number_format_type is not None:
            number_format = {"type": operation.number_format_type}
            if operation.number_format_pattern:
                number_format["pattern"] = operation.number_format_pattern
            cell.setdefault("userEnteredFormat", {})["numberFormat"] = number_format
            fields.append("userEnteredFormat.numberFormat")
        if operation.background_color is not None:
            cell.setdefault("userEnteredFormat", {})[
                "backgroundColor"
            ] = _rgb(operation.background_color)
            fields.append("userEnteredFormat.backgroundColor")
        return {
            "repeatCell": {
                "range": {
                    "sheetId": sheet_id,
                    "startRowIndex": parsed.start_row - 1,
                    "endRowIndex": parsed.end_row,
                    "startColumnIndex": parsed.start_column - 1,
                    "endColumnIndex": parsed.end_column,
                },
                "cell": cell,
                "fields": ",".join(fields),
            }
        }
    raise WorkspaceAdapterError(
        "spreadsheet_operation_invalid", "Unsupported spreadsheet operation.", 422
    )


def _qualified_range(sheet_title: str, local_range: str) -> str:
    """Build provider A1 only after an opaque sheet_ref has been authorized."""

    escaped_title = sheet_title.replace("'", "''")
    return f"'{escaped_title}'!{local_range}"


def _rgb(value: str) -> dict[str, float]:
    return {
        "red": int(value[1:3], 16) / 255,
        "green": int(value[3:5], 16) / 255,
        "blue": int(value[5:7], 16) / 255,
    }
