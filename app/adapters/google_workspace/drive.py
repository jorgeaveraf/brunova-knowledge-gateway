"""Governed access to Google Drive metadata and source-scoped mutations."""

from collections.abc import Callable
from tempfile import SpooledTemporaryFile
from typing import Any

from google.auth.exceptions import GoogleAuthError
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError
from googleapiclient.http import MediaIoBaseDownload, MediaIoBaseUpload

from app.adapters.google_workspace.auth import build_delegated_credentials
from app.adapters.google_workspace.errors import WorkspaceAdapterError, map_google_error
from app.adapters.google_workspace.models import (
    ArtifactMetadata,
    DriveFile,
    SourceMetadata,
    WorkspaceResource,
)
from app.config.settings import Settings
from app.policies.classification import SourceContext
from app.policies.source_access import AllowedSource, SourceAccessPolicy
from app.source_discovery.interface import CandidateSource
from app.source_registry import Classification

ServiceBuilder = Callable[..., Any]

MIME_TYPES = {
    "application/vnd.google-apps.document": "document",
    "application/vnd.google-apps.spreadsheet": "spreadsheet",
    "application/vnd.google-apps.presentation": "presentation",
    "application/vnd.google-apps.folder": "folder",
}
NATIVE_ARTIFACT_MIME_TYPES = {
    "application/vnd.google-apps.document": None,
    "application/vnd.google-apps.spreadsheet": None,
    "application/vnd.google-apps.presentation": None,
}
OFFICE_ARTIFACT_MIME_TYPES = {
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "xlsx",
    "application/vnd.ms-excel.sheet.macroenabled.12": "xlsm",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "pptx",
}
ARTIFACT_METADATA_FIELDS = "files(name,mimeType,size,modifiedTime)"
GOOGLE_DOC_MIME_TYPE = "application/vnd.google-apps.document"
GOOGLE_SHEET_MIME_TYPE = "application/vnd.google-apps.spreadsheet"
GOOGLE_FOLDER_MIME_TYPE = "application/vnd.google-apps.folder"


class GoogleWorkspaceAdapter:
    """Governed Google Workspace adapter using delegated runtime credentials."""

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
        return self._list_allowed_sources(
            sources=(*sources.shared_drives, *sources.folders),
            limit=limit,
            source_policy=source_policy,
        )

    def list_source_files(
        self,
        *,
        source: AllowedSource,
        limit: int,
        source_policy: SourceAccessPolicy,
    ) -> list[DriveFile]:
        """List files for one source already resolved and authorized by policy."""

        return self._list_allowed_sources(
            sources=(source,),
            limit=limit,
            source_policy=source_policy,
        )

    def inspect_source_artifacts(
        self,
        *,
        source: AllowedSource,
        limit: int,
    ) -> list[ArtifactMetadata]:
        """Return safe format metadata for recognized artifacts in one source."""

        try:
            drive = self._drive()
            response = self._list_source_location(
                drive,
                source,
                page_size=limit,
                fields=ARTIFACT_METADATA_FIELDS,
            )
            artifacts: list[ArtifactMetadata] = []
            for item in response.get("files", []):
                mime_type = str(item.get("mimeType", ""))
                normalized_mime_type = mime_type.casefold()
                if normalized_mime_type in NATIVE_ARTIFACT_MIME_TYPES:
                    artifact_type = "native_artifact"
                    extension = None
                elif normalized_mime_type in OFFICE_ARTIFACT_MIME_TYPES:
                    artifact_type = "office_artifact"
                    extension = OFFICE_ARTIFACT_MIME_TYPES[normalized_mime_type]
                else:
                    continue
                raw_size = item.get("size")
                artifacts.append(
                    ArtifactMetadata(
                        name=item.get("name", ""),
                        type=artifact_type,
                        mime_type=mime_type,
                        extension=extension,
                        size=int(raw_size) if raw_size is not None else None,
                        modified_time=item.get("modifiedTime", ""),
                        source_id=source.context.source_id,
                    )
                )
            return artifacts
        except (GoogleAuthError, HttpError) as error:
            raise map_google_error(error) from error

    def _list_allowed_sources(
        self,
        *,
        sources: tuple[AllowedSource, ...],
        limit: int,
        source_policy: SourceAccessPolicy,
    ) -> list[DriveFile]:
        files: list[DriveFile] = []
        seen_ids: set[str] = set()

        try:
            credentials = self._credentials_factory(self._settings)
            drive = self._service_builder(
                "drive", "v3", credentials=credentials, cache_discovery=False
            )
            for allowed_source in sources:
                if len(files) >= limit:
                    break
                response = self._list_source_location(
                    drive,
                    allowed_source,
                    page_size=limit - len(files),
                )
                self._append_allowed_files(
                    response,
                    files,
                    seen_ids,
                    source_policy,
                    allowed_source.context,
                    limit,
                )
            return files
        except (GoogleAuthError, HttpError) as error:
            raise map_google_error(error) from error

    @staticmethod
    def _list_source_location(
        drive: Any,
        source: AllowedSource,
        *,
        page_size: int,
        fields: str = "files(id,name,mimeType)",
    ) -> dict[str, Any]:
        definition = source.definition
        common = {
            "includeItemsFromAllDrives": True,
            "supportsAllDrives": True,
            "pageSize": page_size,
            "fields": fields,
            "orderBy": "modifiedTime desc",
        }
        if definition.location_type == "shared_drive":
            return (
                drive.files()
                .list(
                    corpora="drive",
                    driveId=definition.location_id,
                    q="trashed=false",
                    **common,
                )
                .execute()
            )
        return (
            drive.files()
            .list(
                q=f"'{definition.location_id}' in parents and trashed=false",
                spaces="drive",
                **common,
            )
            .execute()
        )

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
                parent_ids=tuple(metadata.get("parents", [])),
            )
        except (GoogleAuthError, HttpError) as error:
            raise map_google_error(error) from error

    def find_resources(
        self,
        *,
        name: str,
        mime_type: str | None = None,
        source: AllowedSource | None = None,
    ) -> list[WorkspaceResource]:
        """Resolve exact names live in the selected source's Drive corpus."""

        escaped_name = name.replace("\\", "\\\\").replace("'", "\\'")
        query = f"name = '{escaped_name}' and trashed=false"
        if mime_type:
            escaped_mime = mime_type.replace("'", "\\'")
            query += f" and mimeType = '{escaped_mime}'"
        try:
            drive = self._drive()
            parameters: dict[str, Any] = {
                "q": query,
                "spaces": "drive",
                "pageSize": 100,
                "fields": (
                    "nextPageToken,"
                    "files(id,name,mimeType,modifiedTime,driveId,parents)"
                ),
                "includeItemsFromAllDrives": True,
                "supportsAllDrives": True,
                "orderBy": "modifiedTime desc",
            }
            if source is not None:
                drive_id = (
                    source.definition.location_id
                    if source.definition.location_type == "shared_drive"
                    else self.get_resource(source.definition.location_id).drive_id
                )
                if drive_id:
                    parameters.update(corpora="drive", driveId=drive_id)
                else:
                    parameters["corpora"] = "user"

            resources: list[WorkspaceResource] = []
            while True:
                response = drive.files().list(**parameters).execute()
                resources.extend(
                    self.get_resource(str(item["id"]))
                    for item in response.get("files", [])
                )
                page_token = response.get("nextPageToken")
                if not page_token:
                    return resources
                parameters["pageToken"] = page_token
        except (GoogleAuthError, HttpError) as error:
            raise map_google_error(error) from error

    def logical_path(self, resource: WorkspaceResource) -> str:
        """Build a bounded display path for disambiguation; never returned publicly."""

        try:
            drive = self._drive()
            names = [resource.name]
            pending = list(resource.parent_ids[:1])
            visited: set[str] = set()
            while pending and len(names) <= self._settings.workspace_source_max_depth:
                parent_id = pending.pop(0)
                if parent_id in visited:
                    break
                visited.add(parent_id)
                parent = self._get_file_metadata(drive, parent_id)
                parent_name = str(parent.get("name", ""))
                if parent_name:
                    names.append(parent_name)
                pending.extend(parent.get("parents", [])[:1])
            return "/".join(reversed(names))
        except (GoogleAuthError, HttpError) as error:
            raise map_google_error(error) from error

    def copy_resource(
        self,
        *,
        resource: WorkspaceResource,
        name: str,
        destination: WorkspaceResource,
    ) -> WorkspaceResource:
        """Create a native Drive copy in one explicitly authorized folder."""

        if destination.mime_type != GOOGLE_FOLDER_MIME_TYPE:
            raise WorkspaceAdapterError(
                "mutation_destination_invalid",
                "The copy destination must be a Google Drive folder.",
                422,
            )
        try:
            metadata = (
                self._drive()
                .files()
                .copy(
                    fileId=resource.id,
                    body={"name": name, "parents": [destination.id]},
                    fields="id,name,mimeType,modifiedTime,driveId,parents",
                    supportsAllDrives=True,
                )
                .execute()
            )
            return WorkspaceResource(
                id=metadata["id"],
                name=metadata.get("name", name),
                mime_type=metadata.get("mimeType", resource.mime_type),
                modified_time=metadata.get("modifiedTime", ""),
                drive_id=metadata.get("driveId", destination.drive_id),
                ancestor_ids=(destination.id, *destination.ancestor_ids),
                parent_ids=tuple(metadata.get("parents", [destination.id])),
            )
        except (GoogleAuthError, HttpError) as error:
            raise map_google_error(error) from error

    def rename_resource(self, *, resource: WorkspaceResource, name: str) -> WorkspaceResource:
        """Rename one authorized Drive artifact without moving it."""

        try:
            metadata = (
                self._drive()
                .files()
                .update(
                    fileId=resource.id,
                    body={"name": name},
                    fields="id,name,mimeType,modifiedTime,driveId,parents",
                    supportsAllDrives=True,
                )
                .execute()
            )
            return WorkspaceResource(
                id=metadata["id"],
                name=metadata.get("name", name),
                mime_type=metadata.get("mimeType", resource.mime_type),
                modified_time=metadata.get("modifiedTime", resource.modified_time),
                drive_id=metadata.get("driveId", resource.drive_id),
                ancestor_ids=resource.ancestor_ids,
                parent_ids=tuple(metadata.get("parents", resource.parent_ids)),
            )
        except (GoogleAuthError, HttpError) as error:
            raise map_google_error(error) from error

    def create_document(self, *, source: AllowedSource, name: str) -> WorkspaceResource:
        """Create only a native Google Doc at the approved source root."""

        try:
            metadata = (
                self._drive()
                .files()
                .create(
                    body={
                        "name": name,
                        "mimeType": GOOGLE_DOC_MIME_TYPE,
                        "parents": [source.definition.location_id],
                    },
                    fields="id,name,mimeType,modifiedTime,driveId,parents",
                    supportsAllDrives=True,
                )
                .execute()
            )
            return WorkspaceResource(
                id=metadata["id"],
                name=metadata.get("name", name),
                mime_type=metadata.get("mimeType", GOOGLE_DOC_MIME_TYPE),
                modified_time=metadata.get("modifiedTime", ""),
                drive_id=metadata.get("driveId"),
                ancestor_ids=(source.definition.location_id,),
                parent_ids=tuple(metadata.get("parents", [source.definition.location_id])),
            )
        except (GoogleAuthError, HttpError) as error:
            raise map_google_error(error) from error

    def create_spreadsheet(self, *, source: AllowedSource, name: str) -> WorkspaceResource:
        """Create only a native Google Sheet at the approved source root."""

        try:
            metadata = (
                self._drive()
                .files()
                .create(
                    body={
                        "name": name,
                        "mimeType": GOOGLE_SHEET_MIME_TYPE,
                        "parents": [source.definition.location_id],
                    },
                    fields="id,name,mimeType,modifiedTime,driveId,parents",
                    supportsAllDrives=True,
                )
                .execute()
            )
            return WorkspaceResource(
                id=metadata["id"],
                name=metadata.get("name", name),
                mime_type=metadata.get("mimeType", GOOGLE_SHEET_MIME_TYPE),
                modified_time=metadata.get("modifiedTime", ""),
                drive_id=metadata.get("driveId"),
                ancestor_ids=(source.definition.location_id,),
                parent_ids=tuple(metadata.get("parents", [source.definition.location_id])),
            )
        except (GoogleAuthError, HttpError) as error:
            raise map_google_error(error) from error

    def move_resource(
        self,
        *,
        resource: WorkspaceResource,
        destination: WorkspaceResource,
    ) -> WorkspaceResource:
        """Move an artifact to a pre-authorized folder; policy resolves scope."""

        if destination.mime_type != GOOGLE_FOLDER_MIME_TYPE:
            raise WorkspaceAdapterError(
                "mutation_destination_invalid",
                "The move destination must be a Google Drive folder.",
                422,
            )
        if destination.id in resource.parent_ids:
            raise WorkspaceAdapterError(
                "mutation_destination_invalid",
                "The artifact is already in the requested destination.",
                422,
            )
        if not resource.parent_ids:
            raise WorkspaceAdapterError(
                "mutation_destination_invalid",
                "The artifact does not have a movable parent.",
                422,
            )
        try:
            metadata = (
                self._drive()
                .files()
                .update(
                    fileId=resource.id,
                    addParents=destination.id,
                    removeParents=",".join(resource.parent_ids),
                    fields="id,name,mimeType,modifiedTime,driveId,parents",
                    supportsAllDrives=True,
                )
                .execute()
            )
            return WorkspaceResource(
                id=metadata["id"],
                name=metadata.get("name", resource.name),
                mime_type=metadata.get("mimeType", resource.mime_type),
                modified_time=metadata.get("modifiedTime", ""),
                drive_id=metadata.get("driveId", resource.drive_id),
                ancestor_ids=(destination.id, *destination.ancestor_ids),
                parent_ids=tuple(metadata.get("parents", [destination.id])),
            )
        except (GoogleAuthError, HttpError) as error:
            raise map_google_error(error) from error

    def convert_resource(
        self,
        *,
        resource: WorkspaceResource,
        target_mime_type: str,
        target_name: str,
    ) -> WorkspaceResource:
        """Import an Office artifact as a new native Google Workspace artifact."""

        try:
            drive = self._drive()
            media_request = drive.files().get_media(
                fileId=resource.id,
                supportsAllDrives=True,
            )
            with SpooledTemporaryFile(max_size=5 * 1024 * 1024) as media_stream:
                downloader = MediaIoBaseDownload(media_stream, media_request)
                done = False
                while not done:
                    _, done = downloader.next_chunk()
                media_stream.seek(0)
                media = MediaIoBaseUpload(
                    media_stream,
                    mimetype=resource.mime_type,
                    resumable=True,
                )
                metadata = (
                    drive.files()
                    .create(
                        body={
                            "name": target_name,
                            "mimeType": target_mime_type,
                            "parents": list(resource.parent_ids),
                        },
                        media_body=media,
                        fields="id,name,mimeType,modifiedTime,driveId,parents",
                        supportsAllDrives=True,
                    )
                    .execute()
                )
            return WorkspaceResource(
                id=metadata["id"],
                name=metadata.get("name", target_name),
                mime_type=metadata.get("mimeType", target_mime_type),
                modified_time=metadata.get("modifiedTime", ""),
                drive_id=metadata.get("driveId", resource.drive_id),
                ancestor_ids=resource.ancestor_ids,
                parent_ids=tuple(metadata.get("parents", resource.parent_ids)),
            )
        except (GoogleAuthError, HttpError) as error:
            raise map_google_error(error) from error

    def delete_resource(self, *, resource: WorkspaceResource) -> WorkspaceResource:
        """Move an authorized resource to Drive trash for recoverable deletion."""

        try:
            metadata = (
                self._drive()
                .files()
                .update(
                    fileId=resource.id,
                    body={"trashed": True},
                    fields="id,name,mimeType,modifiedTime,driveId,parents",
                    supportsAllDrives=True,
                )
                .execute()
            )
            return WorkspaceResource(
                id=metadata["id"],
                name=metadata.get("name", resource.name),
                mime_type=metadata.get("mimeType", resource.mime_type),
                modified_time=metadata.get("modifiedTime", ""),
                drive_id=metadata.get("driveId", resource.drive_id),
                ancestor_ids=resource.ancestor_ids,
                parent_ids=tuple(metadata.get("parents", resource.parent_ids)),
            )
        except (GoogleAuthError, HttpError) as error:
            raise map_google_error(error) from error

    def share_resource(
        self,
        *,
        resource: WorkspaceResource,
        audience: str,
    ) -> WorkspaceResource:
        """Grant reader access to one explicit audience; never publish publicly."""

        try:
            (
                self._drive()
                .permissions()
                .create(
                    fileId=resource.id,
                    body={
                        "type": "user",
                        "role": "reader",
                        "emailAddress": audience,
                    },
                    fields="id,type,role,emailAddress",
                    sendNotificationEmail=False,
                    supportsAllDrives=True,
                )
                .execute()
            )
            return resource
        except (GoogleAuthError, HttpError) as error:
            raise map_google_error(error) from error

    def discover_source_candidates(
        self,
        *,
        excluded_location_ids: frozenset[str],
        limit: int,
    ) -> list[CandidateSource]:
        """Discover bounded Workspace locations without changing the registry."""

        try:
            drive = self._drive()
            candidates: list[CandidateSource] = []
            shared_drives = (
                drive.drives()
                .list(pageSize=limit, fields="drives(id,name)")
                .execute()
            )
            for item in shared_drives.get("drives", []):
                location_id = item.get("id", "")
                if not location_id or location_id in excluded_location_ids:
                    continue
                candidates.append(
                    CandidateSource(
                        system="google_workspace",
                        location_type="shared_drive",
                        location_id=location_id,
                        name=item.get("name", ""),
                        classification_suggestion=Classification.MANAGEMENT_ONLY,
                        reasons=("new shared drive detected",),
                    )
                )
                if len(candidates) >= limit:
                    return candidates

            remaining = limit - len(candidates)
            root_folders = (
                drive.files()
                .list(
                    q=(
                        "'root' in parents and "
                        "mimeType='application/vnd.google-apps.folder' and "
                        "trashed=false"
                    ),
                    spaces="drive",
                    pageSize=remaining,
                    fields="files(id,name)",
                    orderBy="name",
                )
                .execute()
            )
            for item in root_folders.get("files", []):
                location_id = item.get("id", "")
                if not location_id or location_id in excluded_location_ids:
                    continue
                candidates.append(
                    CandidateSource(
                        system="google_workspace",
                        location_type="folder",
                        location_id=location_id,
                        name=item.get("name", ""),
                        classification_suggestion=Classification.MANAGEMENT_ONLY,
                        reasons=(
                            "unregistered root folder detected",
                            "located under delegated user's Drive root",
                        ),
                    )
                )
                if len(candidates) >= limit:
                    break
            return candidates
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
        source_context: SourceContext,
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
                    source=SourceMetadata(
                        id=source_context.source_id,
                        name=source_context.source_name,
                        classification=source_context.classification.value,
                    ),
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
