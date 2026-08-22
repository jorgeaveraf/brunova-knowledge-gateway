"""Controlled Google Docs retrieval and governed append-only mutation."""

from collections.abc import Callable, Iterable, Iterator
from typing import Any

from google.auth.exceptions import GoogleAuthError
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from app.adapters.google_workspace.auth import build_delegated_credentials
from app.adapters.google_workspace.errors import WorkspaceAdapterError, map_google_error
from app.adapters.google_workspace.models import GoogleDocContent, WorkspaceResource
from app.config.settings import Settings

GOOGLE_DOC_MIME_TYPE = "application/vnd.google-apps.document"


class GoogleDocsAdapter:
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
    def max_chars(self) -> int:
        return self._settings.workspace_doc_max_chars

    def get_document(
        self, resource: WorkspaceResource, *, max_chars: int
    ) -> GoogleDocContent:
        try:
            credentials = self._credentials_factory(self._settings)
            docs = self._service_builder(
                "docs", "v1", credentials=credentials, cache_discovery=False
            )
            if resource.mime_type != GOOGLE_DOC_MIME_TYPE:
                raise WorkspaceAdapterError(
                    "resource_type_invalid",
                    "The requested resource is not a native Google Doc.",
                    422,
                )
            document = (
                docs.documents()
                .get(documentId=resource.id, includeTabsContent=True)
                .execute()
            )
            text, truncated = _bounded_text(_document_text_chunks(document), max_chars)
            return GoogleDocContent(
                id=resource.id,
                name=resource.name,
                mime_type=resource.mime_type,
                modified_time=resource.modified_time,
                text=text,
                truncated=truncated,
                limit=max_chars,
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

    def append_text(self, resource: WorkspaceResource, *, text: str) -> None:
        """Apply the only supported document update: append bounded text."""

        if resource.mime_type != GOOGLE_DOC_MIME_TYPE:
            raise WorkspaceAdapterError(
                "resource_type_invalid",
                "The requested resource is not a native Google Doc.",
                422,
            )
        try:
            credentials = self._credentials_factory(self._settings)
            docs = self._service_builder(
                "docs", "v1", credentials=credentials, cache_discovery=False
            )
            (
                docs.documents()
                .batchUpdate(
                    documentId=resource.id,
                    body={
                        "requests": [
                            {
                                "insertText": {
                                    "endOfSegmentLocation": {},
                                    "text": text,
                                }
                            }
                        ]
                    },
                )
                .execute()
            )
        except (GoogleAuthError, HttpError) as error:
            raise map_google_error(error) from error
        except OSError as error:
            raise WorkspaceAdapterError(
                "credentials_unavailable",
                "Application Default Credentials are not available to the runtime.",
                503,
            ) from error


def _document_text_chunks(document: dict[str, Any]) -> Iterator[str]:
    tabs = document.get("tabs", [])
    if tabs:
        for tab in tabs:
            yield from _tab_text_chunks(tab)
        return
    yield from _structural_text_chunks(document.get("body", {}).get("content", []))


def _tab_text_chunks(tab: dict[str, Any]) -> Iterator[str]:
    document_tab = tab.get("documentTab", {})
    yield from _structural_text_chunks(
        document_tab.get("body", {}).get("content", [])
    )
    for child in tab.get("childTabs", []):
        yield from _tab_text_chunks(child)


def _structural_text_chunks(elements: Iterable[dict[str, Any]]) -> Iterator[str]:
    for element in elements:
        paragraph = element.get("paragraph")
        if paragraph:
            for paragraph_element in paragraph.get("elements", []):
                content = paragraph_element.get("textRun", {}).get("content")
                if content:
                    yield content
        table = element.get("table")
        if table:
            for row in table.get("tableRows", []):
                for cell in row.get("tableCells", []):
                    yield from _structural_text_chunks(cell.get("content", []))
        table_of_contents = element.get("tableOfContents")
        if table_of_contents:
            yield from _structural_text_chunks(table_of_contents.get("content", []))


def _bounded_text(chunks: Iterable[str], limit: int) -> tuple[str, bool]:
    iterator = iter(chunks)
    parts: list[str] = []
    remaining = limit
    for chunk in iterator:
        if len(chunk) > remaining:
            parts.append(chunk[:remaining])
            return "".join(parts), True
        parts.append(chunk)
        remaining -= len(chunk)
        if remaining == 0:
            return "".join(parts), next(iterator, None) is not None
    return "".join(parts), False
