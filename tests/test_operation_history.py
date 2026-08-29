from unittest.mock import Mock

import pytest

from app.adapters.google_cloud_logging import CloudLoggingOperationHistoryStore
from app.adapters.google_workspace.errors import WorkspaceAdapterError
from app.config.settings import Settings
from app.operation_history import (
    AgentSignalOperation,
    GovernedOperation,
    OperationHistoryEntry,
    list_authorized_operation_history,
)
from app.policies.source_access import SourceAccessPolicy
from app.source_registry import SourceDefinition, SourceRegistry, SourceRegistryDocument


def settings(*, blocked=()):
    return Settings(
        workspace_delegated_user="reader@example.com",
        workspace_service_account_email="gateway@project.iam.gserviceaccount.com",
        workspace_doc_max_chars=100,
        workspace_sheet_max_cells=100,
        workspace_blocked_source_ids=blocked,
        workspace_source_max_depth=20,
        workspace_audit_enabled=True,
        workspace_source_registry_path="unused.yaml",
    )


def registry():
    sources = (
        SourceDefinition.model_validate(
            {
                "id": "brunova_template",
                "name": "Brunova Template",
                "system": "google_workspace",
                "location_type": "shared_drive",
                "location_id": "template_drive_123",
                "classification": "management_only",
                "owner": ["Jorge"],
                "status": "active",
                "capabilities": {
                    "read": True,
                    "create": True,
                    "update": True,
                    "move": True,
                    "delete": False,
                    "share": False,
                },
            }
        ),
        SourceDefinition.model_validate(
            {
                "id": "disabled_source",
                "name": "Disabled",
                "system": "google_workspace",
                "location_type": "folder",
                "location_id": "disabled_folder_123",
                "classification": "management_only",
                "owner": ["Jorge"],
                "status": "disabled",
            }
        ),
    )
    return SourceRegistry(SourceRegistryDocument(version=1, sources=sources))


class FakeHistoryStore:
    def __init__(self):
        self.calls = []

    def list(self, *, source_id, operation, limit):
        self.calls.append((source_id, operation, limit))
        return [
            OperationHistoryEntry(
                timestamp="2026-08-22T19:53:06Z",
                operation="create_source_artifact",
                source_id="brunova_template",
                result="success",
                approval_reference="decision-v014-test",
                request_id="request-123",
                correlation_id="request-123",
            ),
            OperationHistoryEntry(
                timestamp="2026-08-22T18:00:00Z",
                operation="move_source_artifact",
                source_id="disabled_source",
                result="success",
                approval_reference="decision-v014-old",
                request_id="request-older",
                correlation_id="request-older",
            ),
        ]


def test_history_filters_to_current_authorized_source_and_limit():
    source_registry = registry()
    store = FakeHistoryStore()

    result = list_authorized_operation_history(
        store=store,
        registry=source_registry,
        source_policy=SourceAccessPolicy(settings(), source_registry),
        source_id="brunova_template",
        operation=GovernedOperation.CREATE_SOURCE_ARTIFACT,
        limit=1,
    )

    assert [entry.source_id for entry in result] == ["brunova_template"]
    assert store.calls == [
        ("brunova_template", GovernedOperation.CREATE_SOURCE_ARTIFACT, 5)
    ]


@pytest.mark.parametrize("limit", [0, 51])
def test_history_rejects_unsafe_limits(limit):
    source_registry = registry()

    with pytest.raises(
        WorkspaceAdapterError,
        match="Operation history limit must be between",
    ):
        list_authorized_operation_history(
            store=FakeHistoryStore(),
            registry=source_registry,
            source_policy=SourceAccessPolicy(settings(), source_registry),
            limit=limit,
        )


def test_cloud_logging_adapter_allowlists_fields_and_drops_sensitive_data():
    response = {
        "entries": [
            {
                "timestamp": "2026-08-22T19:53:06Z",
                "jsonPayload": {
                    "service": "brunova-knowledge-gateway",
                    "action": "update_source_artifact",
                    "source_id": "brunova_template",
                    "result": "success",
                    "approval_reference": "decision-v014-test",
                    "request_id": "request-123",
                    "correlation_id": "correlation-123",
                    "resource_id": "private_drive_document_id",
                    "content": "private document content",
                    "token": "must-not-escape",
                    "headers": {"authorization": "must-not-escape"},
                    "delegated_user": "private-user@example.com",
                    "audience": "private-audience@example.com",
                },
            }
        ]
    }
    execute = Mock(return_value=response)
    list_call = Mock(return_value=Mock(execute=execute))
    service = Mock()
    service.entries.return_value.list = list_call
    store = CloudLoggingOperationHistoryStore(
        service=service,
        project_id="brunova-ai-platform",
    )

    result = store.list(
        source_id="brunova_template",
        operation=GovernedOperation.UPDATE_SOURCE_ARTIFACT,
        limit=10,
    )

    assert result[0].model_dump(mode="json") == {
        "timestamp": "2026-08-22T19:53:06Z",
        "operation": "update_source_artifact",
        "source_id": "brunova_template",
        "result": "success",
        "approval_reference": "decision-v014-test",
        "request_id": "request-123",
        "correlation_id": "correlation-123",
    }
    serialized = result[0].model_dump_json()
    assert "private_drive_document_id" not in serialized
    assert "private document content" not in serialized
    assert "must-not-escape" not in serialized
    assert "private-user@example.com" not in serialized
    assert "private-audience@example.com" not in serialized
    body = list_call.call_args.kwargs["body"]
    assert body["pageSize"] == 10
    assert 'jsonPayload.source_id="brunova_template"' in body["filter"]
    assert 'jsonPayload.action="update_source_artifact"' in body["filter"]


def test_history_filter_includes_governed_document_tab_mutations():
    query = CloudLoggingOperationHistoryStore._filter(None, None)

    for operation in (
        "create_document_tab",
        "rename_document_tab",
        "delete_document_tab",
    ):
        assert f'jsonPayload.action="{operation}"' in query


def test_agent_signal_history_is_content_free_and_provider_scoped():
    response = {
        "entries": [
            {
                "timestamp": "2026-08-29T12:00:00Z",
                "jsonPayload": {
                    "service": "brunova-knowledge-gateway",
                    "provider": "agent_signals",
                    "action": "agent_signal_claimed",
                    "signal_id": "signal-123",
                    "signal_type": "whatsapp_attention_required",
                    "status_transition": "pending->claimed",
                    "result": "success",
                    "request_id": "request-123",
                    "correlation_id": "correlation-123",
                    "preview": "must-not-escape",
                    "phone": "+520000000000",
                    "content": "must-not-escape",
                },
            }
        ]
    }
    execute = Mock(return_value=response)
    list_call = Mock(return_value=Mock(execute=execute))
    service = Mock()
    service.entries.return_value.list = list_call
    store = CloudLoggingOperationHistoryStore(
        service=service, project_id="brunova-ai-platform"
    )

    result = store.list_agent_signals(
        signal_id="signal-123",
        operation=AgentSignalOperation.CLAIMED,
        limit=10,
    )

    assert result[0].model_dump(mode="json") == {
        "timestamp": "2026-08-29T12:00:00Z",
        "operation": "agent_signal_claimed",
        "signal_id": "signal-123",
        "signal_type": "whatsapp_attention_required",
        "status_transition": "pending->claimed",
        "result": "success",
        "request_id": "request-123",
        "correlation_id": "correlation-123",
    }
    serialized = result[0].model_dump_json()
    assert "must-not-escape" not in serialized
    assert "+520000000000" not in serialized
    query = list_call.call_args.kwargs["body"]["filter"]
    assert 'jsonPayload.provider="agent_signals"' in query
    assert 'jsonPayload.signal_id="signal-123"' in query
