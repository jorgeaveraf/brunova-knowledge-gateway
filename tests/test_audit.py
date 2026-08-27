import json
from types import SimpleNamespace
from unittest.mock import Mock

from app.audit import (
    correlation_id,
    emit_audit_event,
    emit_audit_record,
    request_audit_context,
)


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


def test_source_discovery_route_has_audit_context():
    request = SimpleNamespace(
        url=SimpleNamespace(path="/sources/discover"),
        path_params={},
    )

    assert request_audit_context(request) == (
        "discover_source_candidates",
        None,
        "source_discovery",
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
    assert event["correlation_id"] == "request-123"
    assert event["actor"] == "gateway"
    assert event["delegated_user"] == "reader@example.com"
    assert event["action"] == "read_document"
    assert event["source_id"] == "career_ops"
    assert event["classification"] == "management_only"
    assert event["source_classification"] == "management_only"
    assert "text" not in event
    assert "values" not in event
    assert "owner" not in event


def test_conversion_audit_tracks_lifecycle_ids_without_content(monkeypatch):
    monkeypatch.setenv("WORKSPACE_AUDIT_ENABLED", "true")
    log_info = Mock()
    monkeypatch.setattr("app.audit.audit_logger.info", log_info)

    emit_audit_record(
        request_id="request-v017",
        action="convert_source_artifact",
        resource_id="office_artifact_123",
        created_resource_id="google_sheet_123",
        resource_type="office_artifact",
        result="success",
        http_status=200,
        source_id="career_ops",
        source_classification="management_only",
        approval_reference="decision-v017-convert",
    )

    event = json.loads(log_info.call_args.args[0])
    assert event["resource_id"] == "office_artifact_123"
    assert event["created_resource_id"] == "google_sheet_123"
    assert event["approval_reference"] == "decision-v017-convert"
    assert "content" not in event
    assert "token" not in event


def test_discovery_audit_contains_count_without_candidate_details(monkeypatch):
    monkeypatch.setenv("WORKSPACE_AUDIT_ENABLED", "true")
    log_info = Mock()
    monkeypatch.setattr("app.audit.audit_logger.info", log_info)
    request = SimpleNamespace(
        state=SimpleNamespace(
            request_id="discovery-request-123",
            candidate_count=3,
        )
    )

    emit_audit_event(
        request,
        action="discover_source_candidates",
        resource_id=None,
        resource_type="source_discovery",
        result="success",
        http_status=200,
    )

    event = json.loads(log_info.call_args.args[0])
    assert event["candidate_count"] == 3
    assert "candidates" not in event
    assert "location_id" not in event


def test_hubspot_http_audit_includes_provider_without_credentials(monkeypatch):
    monkeypatch.setenv("WORKSPACE_AUDIT_ENABLED", "true")
    log_info = Mock()
    monkeypatch.setattr("app.audit.audit_logger.info", log_info)
    request = SimpleNamespace(state=SimpleNamespace(request_id="hubspot-request-123"))

    emit_audit_event(
        request,
        action="hubspot_oauth_callback",
        resource_id=None,
        resource_type="hubspot_connection",
        result="success",
        http_status=200,
    )

    event = json.loads(log_info.call_args.args[0])
    assert event["provider"] == "hubspot"
    assert "authorization" not in event
    assert "token" not in event


def test_proposal_audit_contains_receipt_without_review_reason(monkeypatch):
    monkeypatch.setenv("WORKSPACE_AUDIT_ENABLED", "true")
    log_info = Mock()
    monkeypatch.setattr("app.audit.audit_logger.info", log_info)

    emit_audit_record(
        request_id="proposal-request-123",
        action="create_source_proposal",
        resource_id="candidate_00000000000000000000000000000000",
        resource_type="source_proposal",
        result="success",
        http_status=200,
        proposal_id="proposal_00000000000000000000000000000000",
    )

    event = json.loads(log_info.call_args.args[0])
    assert event["proposal_id"] == "proposal_00000000000000000000000000000000"
    assert "reason" not in event
    assert "permissions" not in event
    assert "content" not in event


def test_mutation_audit_contains_approval_but_not_change_content(monkeypatch):
    monkeypatch.setenv("WORKSPACE_AUDIT_ENABLED", "true")
    log_info = Mock()
    monkeypatch.setattr("app.audit.audit_logger.info", log_info)

    emit_audit_record(
        request_id="mutation-request-123",
        action="update_source_artifact",
        resource_id="document_123456789",
        resource_type="google_document",
        result="success",
        http_status=200,
        source_id="brunova_template",
        source_classification="management_only",
        approval_reference="decision-v013-test",
        capability="update",
        authorization_mode="external_approval",
    )

    event = json.loads(log_info.call_args.args[0])
    assert event["approval_reference"] == "decision-v013-test"
    assert event["source_id"] == "brunova_template"
    assert event["capability"] == "update"
    assert event["authorization_mode"] == "external_approval"
    assert "change" not in event
    assert "text" not in event


def test_share_audit_contains_normalized_audience_without_permission_details(
    monkeypatch,
):
    monkeypatch.setenv("WORKSPACE_AUDIT_ENABLED", "true")
    log_info = Mock()
    monkeypatch.setattr("app.audit.audit_logger.info", log_info)

    emit_audit_record(
        request_id="share-request-123",
        action="share_source_artifact",
        resource_id="document_123456789",
        resource_type="google_drive_artifact",
        result="success",
        http_status=200,
        source_id="brunova_template",
        source_classification="management_only",
        approval_reference="decision-v015-test",
        audience="reviewer@brunova.mx",
    )

    event = json.loads(log_info.call_args.args[0])
    assert event["audience"] == "reviewer@brunova.mx"
    assert event["approval_reference"] == "decision-v015-test"
    assert "permission_id" not in event
    assert "headers" not in event
    assert "token" not in event
