import json
from types import SimpleNamespace
from unittest.mock import Mock

from app.audit import correlation_id, emit_audit_event, request_audit_context


def test_valid_correlation_id_is_preserved_and_invalid_one_is_replaced():
    assert correlation_id("client-request-123") == "client-request-123"
    assert correlation_id("invalid request id") != "invalid request id"


def test_source_files_route_has_audit_context():
    request = SimpleNamespace(
        url=SimpleNamespace(path="/sources/career_ops/files"),
        path_params={"source_id": "career_ops"},
    )

    assert request_audit_context(request) == (
        "list_files",
        None,
        "google_drive",
    )


def test_source_scoped_content_routes_have_audit_context():
    document = SimpleNamespace(
        url=SimpleNamespace(path="/sources/career_ops/docs/document_123"),
        path_params={"document_id": "document_123"},
    )
    sheet = SimpleNamespace(
        url=SimpleNamespace(path="/sources/career_ops/sheets/spreadsheet_123"),
        path_params={"spreadsheet_id": "spreadsheet_123"},
    )

    assert request_audit_context(document) == (
        "read_document",
        "document_123",
        "google_doc",
    )
    assert request_audit_context(sheet) == (
        "read_sheet_range",
        "spreadsheet_123",
        "google_sheet",
    )


def test_audit_event_contains_metadata_but_no_content(monkeypatch):
    monkeypatch.setenv("WORKSPACE_AUDIT_ENABLED", "true")
    monkeypatch.setenv(
        "WORKSPACE_SERVICE_ACCOUNT_EMAIL", "gateway@project.iam.gserviceaccount.com"
    )
    monkeypatch.setenv("WORKSPACE_DELEGATED_USER", "reader@example.com")
    log_info = Mock()
    monkeypatch.setattr("app.audit.audit_logger.info", log_info)
    request = SimpleNamespace(
        state=SimpleNamespace(
            request_id="request-123",
            source_id="career_ops",
            classification="management_only",
        )
    )

    emit_audit_event(
        request,
        action="read_document",
        resource_id="document_123456789",
        resource_type="google_doc",
        result="success",
        http_status=200,
    )

    event = json.loads(log_info.call_args.args[0])
    assert event["request_id"] == "request-123"
    assert event["actor"] == "gateway"
    assert event["delegated_user"] == "reader@example.com"
    assert event["action"] == "read_document"
    assert event["source_id"] == "career_ops"
    assert event["classification"] == "management_only"
    assert event["source_classification"] == "management_only"
    assert "text" not in event
    assert "values" not in event
    assert "owner" not in event
