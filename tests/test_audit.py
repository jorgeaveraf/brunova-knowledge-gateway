import json
from types import SimpleNamespace
from unittest.mock import Mock

from app.audit import correlation_id, emit_audit_event


def test_valid_correlation_id_is_preserved_and_invalid_one_is_replaced():
    assert correlation_id("client-request-123") == "client-request-123"
    assert correlation_id("invalid request id") != "invalid request id"


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
    assert "text" not in event
    assert "values" not in event
    assert "owner" not in event
