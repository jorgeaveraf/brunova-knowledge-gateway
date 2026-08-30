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

    def encode_asset(
        self, *, source_id: str, artifact_id: str, mime_type: str
    ) -> str:
        """Return an opaque visual-asset handle bound to source, file, and MIME."""

        nonce = os.urandom(12)
        payload = json.dumps(
            {
                "v": 1,
                "source_id": source_id,
                "artifact_id": artifact_id,
                "mime_type": mime_type,
            },
            separators=(",", ":"),
        ).encode()
        encrypted = AESGCM(self._key).encrypt(
            nonce, payload, b"brunova-visual-asset-ref"
        )
        token = base64.urlsafe_b64encode(nonce + encrypted).decode().rstrip("=")
        return f"asset_{token}"

    def decode_asset(self, asset_ref: str, *, source_id: str) -> tuple[str, str]:
        """Resolve an asset only in the source and MIME context that issued it."""

        try:
            prefix = "asset_"
            if not asset_ref.startswith(prefix):
                raise ValueError
            encoded = asset_ref[len(prefix) :]
            raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            payload = AESGCM(self._key).decrypt(
                raw[:12], raw[12:], b"brunova-visual-asset-ref"
            )
            claims = json.loads(payload)
            if claims.get("v") != 1 or claims.get("source_id") != source_id:
                raise ValueError
            artifact_id = claims.get("artifact_id")
            mime_type = claims.get("mime_type")
            if not isinstance(artifact_id, str) or not artifact_id:
                raise ValueError
            if not isinstance(mime_type, str) or not mime_type:
                raise ValueError
            return artifact_id, mime_type
        except Exception as error:
            raise WorkspaceAdapterError(
                "asset_reference_invalid",
                "The asset reference is invalid for the selected source.",
                403,
            ) from error

    def encode_docx_anchor(
        self,
        *,
        source_id: str,
        artifact_id: str,
        part: str,
        kind: str,
        indexes: list[int],
    ) -> str:
        nonce = os.urandom(12)
        payload = json.dumps(
            {
                "v": 1,
                "source_id": source_id,
                "artifact_id": artifact_id,
                "part": part,
                "kind": kind,
                "indexes": indexes,
            },
            separators=(",", ":"),
        ).encode()
        encrypted = AESGCM(self._key).encrypt(
            nonce, payload, b"brunova-docx-anchor-ref"
        )
        token = base64.urlsafe_b64encode(nonce + encrypted).decode().rstrip("=")
        return f"docx_anchor_{token}"

    def decode_docx_anchor(
        self, anchor: str, *, source_id: str, artifact_id: str
    ) -> tuple[str, str, tuple[int, ...]]:
        try:
            prefix = "docx_anchor_"
            if not anchor.startswith(prefix):
                raise ValueError
            encoded = anchor[len(prefix) :]
            raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            payload = json.loads(
                AESGCM(self._key).decrypt(
                    raw[:12], raw[12:], b"brunova-docx-anchor-ref"
                )
            )
            if (
                payload.get("v") != 1
                or payload.get("source_id") != source_id
                or payload.get("artifact_id") != artifact_id
            ):
                raise ValueError
            part, kind, indexes = payload.get("part"), payload.get("kind"), payload.get("indexes")
            if not isinstance(part, str) or not isinstance(kind, str):
                raise ValueError
            if not isinstance(indexes, list) or not all(isinstance(i, int) and i >= 0 for i in indexes):
                raise ValueError
            return part, kind, tuple(indexes)
        except Exception as error:
            raise WorkspaceAdapterError(
                "docx_anchor_invalid",
                "The DOCX structural anchor is invalid for the selected artifact.",
                403,
            ) from error

    def encode_document_image(
        self, *, source_id: str, artifact_id: str, object_id: str, tab_id: str = ""
    ) -> str:
        nonce = os.urandom(12)
        payload = json.dumps(
            {"v": 1, "source_id": source_id, "artifact_id": artifact_id, "object_id": object_id, "tab_id": tab_id},
            separators=(",", ":"),
        ).encode()
        encrypted = AESGCM(self._key).encrypt(nonce, payload, b"brunova-document-image-ref")
        return "doc_image_" + base64.urlsafe_b64encode(nonce + encrypted).decode().rstrip("=")

    def decode_document_image(
        self, image_ref: str, *, source_id: str, artifact_id: str
    ) -> tuple[str, str]:
        try:
            prefix = "doc_image_"
            if not image_ref.startswith(prefix):
                raise ValueError
            encoded = image_ref[len(prefix) :]
            raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
            payload = json.loads(AESGCM(self._key).decrypt(raw[:12], raw[12:], b"brunova-document-image-ref"))
            if payload.get("v") != 1 or payload.get("source_id") != source_id or payload.get("artifact_id") != artifact_id:
                raise ValueError
            object_id, tab_id = payload.get("object_id"), payload.get("tab_id", "")
            if not isinstance(object_id, str) or not object_id or not isinstance(tab_id, str):
                raise ValueError
            return object_id, tab_id
        except Exception as error:
            raise WorkspaceAdapterError(
                "document_image_reference_invalid",
                "The image reference is invalid for the selected document.",
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
