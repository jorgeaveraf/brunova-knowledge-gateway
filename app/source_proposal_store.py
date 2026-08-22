"""Durable YAML Source Proposal store backed by a versioned Cloud Storage object."""

from __future__ import annotations

from typing import Protocol

import yaml
from google.api_core.exceptions import GoogleAPIError, NotFound, PreconditionFailed
from google.cloud import storage
from pydantic import ValidationError

from app.adapters.google_workspace.errors import WorkspaceAdapterError
from app.source_governance import (
    SourceProposal,
    SourceProposalDocument,
    SourceProposalRecord,
)

MAX_WRITE_ATTEMPTS = 5


class ProposalObjectConflict(RuntimeError):
    """The YAML object changed between read and conditional write."""


class ProposalObjectBackend(Protocol):
    def read(self) -> tuple[str | None, int]: ...

    def write(self, content: str, *, generation: int) -> None: ...


class SourceProposalStore(Protocol):
    def create(self, proposal: SourceProposal) -> SourceProposalRecord: ...

    def list(self) -> tuple[SourceProposalRecord, ...]: ...

    def get(self, proposal_id: str) -> SourceProposalRecord: ...


class CloudStorageProposalObjectBackend:
    def __init__(
        self,
        *,
        bucket_name: str,
        object_name: str,
        client: storage.Client | None = None,
    ) -> None:
        if not bucket_name:
            raise ValueError("SOURCE_PROPOSAL_BUCKET must be configured")
        if not object_name or object_name.startswith("/") or ".." in object_name:
            raise ValueError("SOURCE_PROPOSAL_OBJECT is invalid")
        storage_client = client or storage.Client()
        self._blob = storage_client.bucket(bucket_name).blob(object_name)

    def read(self) -> tuple[str | None, int]:
        try:
            content = self._blob.download_as_text(encoding="utf-8")
            return content, int(self._blob.generation or 0)
        except NotFound:
            return None, 0
        except GoogleAPIError as error:
            raise _store_unavailable() from error

    def write(self, content: str, *, generation: int) -> None:
        try:
            self._blob.upload_from_string(
                content,
                content_type="application/yaml; charset=utf-8",
                if_generation_match=generation,
            )
        except PreconditionFailed as error:
            raise ProposalObjectConflict from error
        except GoogleAPIError as error:
            raise _store_unavailable() from error


class YamlSourceProposalStore:
    """Atomic whole-document proposal registry suitable for a small review queue."""

    def __init__(self, backend: ProposalObjectBackend) -> None:
        self._backend = backend

    def create(self, proposal: SourceProposal) -> SourceProposalRecord:
        record = SourceProposalRecord.from_proposal(proposal)
        for _attempt in range(MAX_WRITE_ATTEMPTS):
            document, generation = self._load()
            if any(item.proposal_id == record.proposal_id for item in document.proposals):
                return record
            updated = SourceProposalDocument(
                proposals=(*document.proposals, record),
            )
            try:
                self._backend.write(
                    _serialize(updated),
                    generation=generation,
                )
                return record
            except ProposalObjectConflict:
                continue
        raise WorkspaceAdapterError(
            "source_proposal_store_conflict",
            "Source proposal registry changed too frequently; retry the request.",
            409,
        )

    def list(self) -> tuple[SourceProposalRecord, ...]:
        document, _generation = self._load()
        return document.proposals

    def get(self, proposal_id: str) -> SourceProposalRecord:
        for proposal in self.list():
            if proposal.proposal_id == proposal_id:
                return proposal
        raise WorkspaceAdapterError(
            "source_proposal_not_found",
            "The requested source proposal was not found.",
            404,
        )

    def _load(self) -> tuple[SourceProposalDocument, int]:
        content, generation = self._backend.read()
        if content is None:
            return SourceProposalDocument(), generation
        try:
            raw_document = yaml.safe_load(content)
            return SourceProposalDocument.model_validate(raw_document), generation
        except (yaml.YAMLError, ValidationError, TypeError) as error:
            raise WorkspaceAdapterError(
                "source_proposal_store_invalid",
                "Source proposal registry is invalid.",
                503,
            ) from error


def _serialize(document: SourceProposalDocument) -> str:
    return yaml.safe_dump(
        document.model_dump(mode="json"),
        allow_unicode=True,
        sort_keys=False,
    )


def _store_unavailable() -> WorkspaceAdapterError:
    return WorkspaceAdapterError(
        "source_proposal_store_unavailable",
        "Source proposal registry is unavailable.",
        503,
    )
