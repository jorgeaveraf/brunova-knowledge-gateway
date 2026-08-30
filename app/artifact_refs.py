"""Stateless encrypted references for source-scoped Workspace artifacts and tabs."""

from __future__ import annotations

import base64
import hashlib
import json
import os
from dataclasses import dataclass

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from app.adapters.google_workspace.errors import WorkspaceAdapterError


@dataclass(frozen=True, repr=False)
class ArtifactReferenceCodec:
    _key: bytes

    @classmethod
    def from_environment(cls) -> "ArtifactReferenceCodec":
        token = os.getenv("BRUNOVA_GATEWAY_TOKEN", "").strip()
        if not token:
            raise WorkspaceAdapterError(
                "artifact_reference_unavailable",
                "Artifact references are not configured.",
                503,
            )
        return cls(hashlib.sha256(b"brunova-artifact-ref-v1\0" + token.encode()).digest())

    @classmethod
    def for_testing(cls, secret: str = "non-production-reference-key") -> "ArtifactReferenceCodec":
        return cls(hashlib.sha256(secret.encode()).digest())

    def encode(self, *, source_id: str, artifact_id: str) -> str:
        nonce = os.urandom(12)
        payload = json.dumps(
            {"v": 1, "source_id": source_id, "artifact_id": artifact_id},
            separators=(",", ":"),
        ).encode()
        encrypted = AESGCM(self._key).encrypt(nonce, payload, b"brunova-artifact-ref")
        token = base64.urlsafe_b64encode(nonce + encrypted).decode().rstrip("=")
        return f"artifact_{token}"

    def decode(self, artifact_ref: str, *, source_id: str) -> str:
        try:
            prefix = "artifact_"
            if not artifact_ref.startswith(prefix):
                raise ValueError
            encoded = artifact_ref[len(prefix) :]
            raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            payload = AESGCM(self._key).decrypt(
                raw[:12], raw[12:], b"brunova-artifact-ref"
            )
            claims = json.loads(payload)
            if claims.get("v") != 1 or claims.get("source_id") != source_id:
                raise ValueError
            artifact_id = claims.get("artifact_id")
            if not isinstance(artifact_id, str) or not artifact_id:
                raise ValueError
            return artifact_id
        except Exception as error:
            raise WorkspaceAdapterError(
                "artifact_reference_invalid",
                "The artifact reference is invalid for the selected source.",
                403,
            ) from error

    def encode_tab(self, *, source_id: str, artifact_id: str, tab_id: str) -> str:
        """Return an opaque tab handle bound to one source and artifact."""

        nonce = os.urandom(12)
        payload = json.dumps(
            {
                "v": 1,
                "source_id": source_id,
                "artifact_id": artifact_id,
                "tab_id": tab_id,
            },
            separators=(",", ":"),
        ).encode()
        encrypted = AESGCM(self._key).encrypt(
            nonce, payload, b"brunova-document-tab-ref"
        )
        token = base64.urlsafe_b64encode(nonce + encrypted).decode().rstrip("=")
        return f"tab_{token}"

    def decode_tab(
        self, tab_ref: str, *, source_id: str, artifact_id: str
    ) -> str:
        """Resolve an opaque tab handle only for its bound source and artifact."""

        try:
            prefix = "tab_"
            if not tab_ref.startswith(prefix):
                raise ValueError
            encoded = tab_ref[len(prefix) :]
            raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            payload = AESGCM(self._key).decrypt(
                raw[:12], raw[12:], b"brunova-document-tab-ref"
            )
            claims = json.loads(payload)
            if (
                claims.get("v") != 1
                or claims.get("source_id") != source_id
                or claims.get("artifact_id") != artifact_id
            ):
                raise ValueError
            tab_id = claims.get("tab_id")
            if not isinstance(tab_id, str) or not tab_id:
                raise ValueError
            return tab_id
        except Exception as error:
            raise WorkspaceAdapterError(
                "document_tab_reference_invalid",
                "The document tab reference is invalid for the selected artifact.",
                403,
            ) from error

    def encode_sheet(self, *, source_id: str, artifact_id: str, sheet_id: str) -> str:
        """Return an opaque sheet handle bound to one source and spreadsheet."""

        nonce = os.urandom(12)
        payload = json.dumps(
            {
                "v": 1,
                "source_id": source_id,
                "artifact_id": artifact_id,
                "sheet_id": sheet_id,
            },
            separators=(",", ":"),
        ).encode()
        encrypted = AESGCM(self._key).encrypt(
            nonce, payload, b"brunova-spreadsheet-sheet-ref"
        )
        token = base64.urlsafe_b64encode(nonce + encrypted).decode().rstrip("=")
        return f"sheet_{token}"

    def decode_sheet(
        self, sheet_ref: str, *, source_id: str, artifact_id: str
    ) -> str:
        """Resolve a sheet handle only for its bound source and spreadsheet."""

        try:
            prefix = "sheet_"
            if not sheet_ref.startswith(prefix):
                raise ValueError
            encoded = sheet_ref[len(prefix) :]
            raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            payload = AESGCM(self._key).decrypt(
                raw[:12], raw[12:], b"brunova-spreadsheet-sheet-ref"
            )
            claims = json.loads(payload)
            if (
                claims.get("v") != 1
                or claims.get("source_id") != source_id
                or claims.get("artifact_id") != artifact_id
            ):
                raise ValueError
            sheet_id = claims.get("sheet_id")
            if not isinstance(sheet_id, str) or not sheet_id:
                raise ValueError
            return sheet_id
        except Exception as error:
            raise WorkspaceAdapterError(
                "spreadsheet_sheet_reference_invalid",
                "The sheet reference is invalid for the selected spreadsheet.",
                403,
            ) from error
