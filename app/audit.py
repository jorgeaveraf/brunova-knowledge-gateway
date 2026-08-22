"""Structured operational audit events without Workspace content."""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from fastapi import Request

SERVICE_NAME = "brunova-knowledge-gateway"
CORRELATION_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

audit_logger = logging.getLogger("brunova.audit")
if not audit_logger.handlers:
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(message)s"))
    audit_logger.addHandler(handler)
audit_logger.setLevel(logging.INFO)
audit_logger.propagate = False


def correlation_id(incoming: str | None) -> str:
    if incoming and CORRELATION_ID_PATTERN.fullmatch(incoming):
        return incoming
    return str(uuid4())


def request_audit_context(request: Request) -> tuple[str | None, str | None, str | None]:
    path = request.url.path
    if path == "/workspace/drive/list":
        return "list_files", None, "google_drive"
    if path == "/sources/discover":
        return "discover_sources", None, "google_drive"
    if path.startswith("/sources/") and path.endswith("/files"):
        return "list_files", None, "google_drive"
    if path.startswith("/sources/") and "/docs/" in path:
        return "read_document", request.path_params.get("document_id"), "google_doc"
    if path.startswith("/sources/") and "/sheets/" in path:
        return (
            "read_sheet_range",
            request.path_params.get("spreadsheet_id"),
            "google_sheet",
        )
    if path.startswith("/workspace/docs/"):
        return "read_document", request.path_params.get("document_id"), "google_doc"
    if path.startswith("/workspace/sheets/"):
        return (
            "read_sheet_range",
            request.path_params.get("spreadsheet_id"),
            "google_sheet",
        )
    return None, None, None


def emit_audit_event(
    request: Request,
    *,
    action: str,
    resource_id: str | None,
    resource_type: str | None,
    result: str,
    http_status: int,
    error_code: str | None = None,
) -> None:
    emit_audit_record(
        request_id=request.state.request_id,
        action=action,
        resource_id=resource_id,
        resource_type=resource_type,
        result=result,
        http_status=http_status,
        error_code=error_code,
        source_id=getattr(request.state, "source_id", None),
        source_classification=getattr(request.state, "classification", None),
    )


def emit_audit_record(
    *,
    request_id: str,
    action: str,
    resource_id: str | None,
    resource_type: str | None,
    result: str,
    http_status: int,
    error_code: str | None = None,
    source_id: str | list[str] | None = None,
    source_classification: str | list[str] | None = None,
    consumer: str | None = None,
) -> None:
    if os.getenv("WORKSPACE_AUDIT_ENABLED", "true").strip().lower() not in (
        "1",
        "true",
        "yes",
        "on",
    ):
        return
    service_account = os.getenv("WORKSPACE_SERVICE_ACCOUNT_EMAIL", "")
    event: dict[str, Any] = {
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "service": SERVICE_NAME,
        "actor": service_account.split("@", 1)[0],
        "delegated_user": os.getenv("WORKSPACE_DELEGATED_USER", ""),
        "action": action,
        "resource_id": resource_id,
        "resource_type": resource_type,
        "result": result,
        "http_status": http_status,
        "request_id": request_id,
        "source_id": source_id,
        "classification": source_classification,
        "source_classification": source_classification,
    }
    if consumer:
        event["consumer"] = consumer
    if error_code:
        event["error_code"] = error_code
    audit_logger.info(json.dumps(event, separators=(",", ":"), sort_keys=True))
