"""Stateless encrypted references for source-scoped Workspace artifacts."""

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
