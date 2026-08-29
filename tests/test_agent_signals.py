import asyncio
import base64
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi.testclient import TestClient
from mcp import Client

import app.main as main_module
import app.mcp_server as mcp_module
from app.adapters.google_workspace.errors import WorkspaceAdapterError
from app.agent_signals import (
    AgentSignalInbox,
    AgentSignalObjectConflict,
    AgentSignalPayload,
    AgentSignalRecord,
    SignalPriority,
    SignalStatus,
    StoredSignal,
    CloudStorageAgentSignalBackend,
)
from app.auth.pubsub_push import verify_pubsub_oidc
from app.auth.principals import (
    CapabilityScope,
    Principal,
    ProviderScope,
    bind_principal,
    reset_principal,
)


class MemorySignalBackend:
    def __init__(self):
        self.items = {}
        self.fail_create_count = 0

    def create(self, record):
        if self.fail_create_count:
            self.fail_create_count -= 1
            raise WorkspaceAdapterError(
                "agent_signal_store_unavailable", "store unavailable", 503
            )
        if record.signal_id in self.items:
            return False
        self.items[record.signal_id] = (record, 1)
        return True

    def read(self, signal_id):
        try:
            record, generation = self.items[signal_id]
        except KeyError as error:
            raise WorkspaceAdapterError(
                "agent_signal_not_found", "signal not found", 404
            ) from error
        return StoredSignal(record=record, generation=generation)

    def list(self):
        return [
            StoredSignal(record=record, generation=generation)
            for record, generation in self.items.values()
        ]

    def replace(self, record, *, generation):
        current = self.items.get(record.signal_id)
        if current is None or current[1] != generation:
            raise AgentSignalObjectConflict
        self.items[record.signal_id] = (record, generation + 1)

    def delete(self, signal_id, *, generation):
        current = self.items.get(signal_id)
        if current and current[1] == generation:
            del self.items[signal_id]


def signal_dict(
    signal_id="sig-001",
    *,
    priority="attention",
    occurred_at="2026-08-29T12:00:00Z",
):
    return {
        "signal_id": signal_id,
        "signal_type": "whatsapp_attention_required",
        "priority": priority,
        "occurred_at": occurred_at,
        "source": "openwa",
        "reason": {
            "classification": "needs_human",
            "summary": "A controlled synthetic test signal.",
            "confidence": 1,
        },
        "contact": {
            "sender_phone": "+520000000000",
            "sender_name": "Synthetic Test",
            "hubspot_contact_id": "test-contact",
        },
        "conversation": {
            "session_id": "test-session",
            "chat_id": "test-chat",
            "message_id": "test-message",
        },
        "preview": "Synthetic preview only",
        "metadata": {"chat_type": "direct", "is_test": True},
    }


def payload(**kwargs):
    return AgentSignalPayload.model_validate(signal_dict(**kwargs))


def receive(inbox, item, message_id="pubsub-1"):
    return inbox.receive(
        item,
        message_id=message_id,
        publish_time=datetime(2026, 8, 29, 12, 1, tzinfo=timezone.utc),
        attributes={"producer": "synthetic-test"},
    )


def push_envelope(item, message_id="pubsub-1"):
    raw = item.model_dump_json().encode()
    return {
        "message": {
            "data": base64.b64encode(raw).decode(),
            "messageId": message_id,
            "publishTime": "2026-08-29T12:01:00Z",
            "attributes": {"producer": "synthetic-test"},
        },
        "subscription": "projects/test/subscriptions/test",
    }


def test_a_valid_signal_is_pending_and_b_duplicate_is_one_item():
    backend = MemorySignalBackend()
    inbox = AgentSignalInbox(backend)

    first = receive(inbox, payload())
    duplicate = receive(inbox, payload(), message_id="pubsub-redelivery")

    assert first.created is True
    assert duplicate.created is False
    assert len(backend.items) == 1
    assert inbox.get("sig-001").status == SignalStatus.PENDING
    assert inbox.get("sig-001").references == {
        "session_id": "test-session",
        "chat_id": "test-chat",
        "message_id": "test-message",
        "hubspot_contact_id": "test-contact",
    }


def test_c_invalid_common_and_whatsapp_specific_schema_are_rejected():
    invalid = signal_dict()
    invalid["signal_id"] = ""
    with pytest.raises(ValueError):
        AgentSignalPayload.model_validate(invalid)

    invalid = signal_dict()
    del invalid["contact"]
    with pytest.raises(ValueError):
        AgentSignalPayload.model_validate(invalid)

    invalid = signal_dict()
    del invalid["conversation"]
    with pytest.raises(ValueError):
        AgentSignalPayload.model_validate(invalid)

    # Future types need only the common contract, not WhatsApp fields.
    future = {
        "signal_id": "lead-1",
        "signal_type": "lead_followup_required",
        "priority": "urgent",
        "occurred_at": "2026-08-29T12:00:00Z",
        "source": "hubspot",
        "reason": {"summary": "Follow up"},
        "references": {"contact_id": "123"},
        "metadata": {},
    }
    assert AgentSignalPayload.model_validate(future).contact is None


def test_whatsapp_hubspot_enrichment_accepts_value_null_absent_and_empty():
    enriched = AgentSignalPayload.model_validate(signal_dict())

    null_data = signal_dict(signal_id="sig-null")
    null_data["contact"]["hubspot_contact_id"] = None
    null_signal = AgentSignalPayload.model_validate(null_data)

    absent_data = signal_dict(signal_id="sig-absent")
    del absent_data["contact"]["hubspot_contact_id"]
    absent_signal = AgentSignalPayload.model_validate(absent_data)

    empty_data = signal_dict(signal_id="sig-empty")
    empty_data["contact"]["hubspot_contact_id"] = "  \t "
    empty_signal = AgentSignalPayload.model_validate(empty_data)

    assert enriched.contact.hubspot_contact_id == "test-contact"
    assert enriched.references["hubspot_contact_id"] == "test-contact"
    for signal in (null_signal, absent_signal, empty_signal):
        assert signal.contact.hubspot_contact_id is None
        assert "hubspot_contact_id" not in signal.references


def test_whatsapp_hubspot_enrichment_normalizes_a_real_identifier():
    data = signal_dict(signal_id="sig-normalized")
    data["contact"]["hubspot_contact_id"] = "  contact-123  "

    signal = AgentSignalPayload.model_validate(data)

    assert signal.contact.hubspot_contact_id == "contact-123"
    assert signal.references["hubspot_contact_id"] == "contact-123"


def test_e_f_g_list_orders_urgent_then_oldest_and_gets_by_id():
    inbox = AgentSignalInbox(MemorySignalBackend())
    receive(inbox, payload(signal_id="attention-old", occurred_at="2026-08-28T12:00:00Z"))
    receive(inbox, payload(signal_id="urgent-new", priority="urgent", occurred_at="2026-08-29T12:00:00Z"))
    receive(inbox, payload(signal_id="urgent-old", priority="urgent", occurred_at="2026-08-27T12:00:00Z"))

    assert [item.signal_id for item in inbox.list()] == [
        "urgent-old",
        "urgent-new",
        "attention-old",
    ]
    assert inbox.get("urgent-new").priority == SignalPriority.URGENT


def test_h_i_j_k_l_claim_conflict_release_complete_and_dismiss():
    inbox = AgentSignalInbox(MemorySignalBackend())
    receive(inbox, payload(signal_id="complete-me"))
    receive(inbox, payload(signal_id="dismiss-me"))

    assert inbox.claim("complete-me", principal_id="management").status == SignalStatus.CLAIMED
    with pytest.raises(WorkspaceAdapterError) as conflict:
        inbox.claim("complete-me", principal_id="other-agent")
    assert conflict.value.code == "agent_signal_already_claimed"
    assert inbox.release("complete-me").status == SignalStatus.PENDING
    inbox.claim("complete-me", principal_id="management")
    completed = inbox.complete(
        "complete-me",
        completion_summary="Synthetic operation completed.",
        outcome_metadata={"outcome": "test_success"},
    )
    dismissed = inbox.dismiss("dismiss-me", reason="Synthetic duplicate was unnecessary.")

    assert completed.status == SignalStatus.COMPLETED
    assert dismissed.status == SignalStatus.DISMISSED
    with pytest.raises(WorkspaceAdapterError):
        inbox.release("complete-me")


def test_m_expired_claim_is_reclaimable():
    now = datetime(2026, 8, 29, 12, tzinfo=timezone.utc)
    clock = [now]
    inbox = AgentSignalInbox(
        MemorySignalBackend(), claim_lease_seconds=60, clock=lambda: clock[0]
    )
    receive(inbox, payload())
    inbox.claim("sig-001", principal_id="agent-a")
    clock[0] += timedelta(seconds=61)

    reclaimed = inbox.claim("sig-001", principal_id="agent-b")

    assert reclaimed.claimed_by == "agent-b"
    assert reclaimed.claimed_at == clock[0]


def test_completed_and_dismissed_cleanup_after_retention():
    now = datetime(2026, 8, 1, tzinfo=timezone.utc)
    clock = [now]
    backend = MemorySignalBackend()
    inbox = AgentSignalInbox(backend, completed_retention_days=30, clock=lambda: clock[0])
    receive(inbox, payload())
    inbox.claim("sig-001", principal_id="management")
    inbox.complete("sig-001", completion_summary="done")
    clock[0] += timedelta(days=31)

    assert inbox.cleanup() == 1
    assert backend.items == {}


def test_q_transient_persistence_failure_can_redeliver_once():
    backend = MemorySignalBackend()
    backend.fail_create_count = 1
    inbox = AgentSignalInbox(backend)
    with pytest.raises(WorkspaceAdapterError):
        receive(inbox, payload())

    assert receive(inbox, payload()).created is True
    assert len(backend.items) == 1


def test_d_push_endpoint_denies_invalid_auth(monkeypatch):
    def denied(_authorization):
        raise WorkspaceAdapterError(
            "invalid_pubsub_authentication", "invalid", 401
        )

    monkeypatch.setattr(main_module, "verify_pubsub_oidc", denied)
    response = TestClient(main_module.app).post(
        "/events/agent-signals", json=push_envelope(payload())
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "invalid_pubsub_authentication"


def test_a_b_c_r_push_endpoint_persists_dedupes_and_acks_invalid(monkeypatch):
    inbox = AgentSignalInbox(MemorySignalBackend())
    monkeypatch.setattr(main_module, "verify_pubsub_oidc", lambda _auth: {})
    monkeypatch.setattr(main_module, "get_agent_signal_inbox", lambda: inbox)
    client = TestClient(main_module.app)
    envelope = push_envelope(payload())

    first = client.post("/events/agent-signals", json=envelope)
    duplicate = client.post("/events/agent-signals", json=envelope)
    invalid = client.post(
        "/events/agent-signals",
        json={"message": {"data": "not-base64", "messageId": "bad"}},
    )

    assert first.status_code == duplicate.status_code == invalid.status_code == 204
    assert len(inbox.list()) == 1


def test_q_push_endpoint_returns_failure_then_accepts_redelivery(monkeypatch):
    backend = MemorySignalBackend()
    backend.fail_create_count = 1
    inbox = AgentSignalInbox(backend)
    monkeypatch.setattr(main_module, "verify_pubsub_oidc", lambda _auth: {})
    monkeypatch.setattr(main_module, "get_agent_signal_inbox", lambda: inbox)
    client = TestClient(main_module.app)
    envelope = push_envelope(payload())

    assert client.post("/events/agent-signals", json=envelope).status_code == 503
    assert client.post("/events/agent-signals", json=envelope).status_code == 204
    assert len(backend.items) == 1


def test_n_o_p_management_sees_tools_and_developer_cannot_invoke(monkeypatch):
    async def management_catalog():
        async with Client(mcp_module.mcp_server) as client:
            return await client.list_tools()

    names = {tool.name for tool in asyncio.run(management_catalog()).tools}
    assert {
        "list_agent_signals",
        "get_agent_signal",
        "claim_agent_signal",
        "complete_agent_signal",
        "dismiss_agent_signal",
        "release_agent_signal",
        "agent_signal_status",
        "get_agent_signal_operation_history",
    } <= names

    developer = Principal(
        id="dev_test",
        type="developer",
        status="active",
        providers=ProviderScope(workspace=True),
        sources=frozenset({"career_ops"}),
        capabilities=CapabilityScope(read=True),
    )

    async def developer_attempt():
        token = bind_principal(developer)
        try:
            async with Client(mcp_module.mcp_server) as client:
                tools = await client.list_tools()
                result = await client.call_tool("list_agent_signals", {})
                return tools, result
        finally:
            reset_principal(token)

    tools, denied = asyncio.run(developer_attempt())
    assert not any("agent_signal" in tool.name for tool in tools.tools)
    assert denied.is_error is True
    assert "tool_denied" in denied.content[0].text


def test_management_mcp_tools_drive_the_lifecycle(monkeypatch):
    inbox = AgentSignalInbox(MemorySignalBackend())
    receive(inbox, payload(signal_id="mcp-complete"))
    receive(inbox, payload(signal_id="mcp-dismiss"))
    runtime = SimpleNamespace(agent_signal_inbox=inbox)
    monkeypatch.setattr(mcp_module, "get_runtime_gateway", lambda: runtime)

    async def scenario():
        async with Client(mcp_module.mcp_server) as client:
            listed = await client.call_tool("list_agent_signals", {})
            fetched = await client.call_tool(
                "get_agent_signal", {"signal_id": "mcp-complete"}
            )
            claimed = await client.call_tool(
                "claim_agent_signal", {"signal_id": "mcp-complete"}
            )
            released = await client.call_tool(
                "release_agent_signal", {"signal_id": "mcp-complete"}
            )
            await client.call_tool(
                "claim_agent_signal", {"signal_id": "mcp-complete"}
            )
            completed = await client.call_tool(
                "complete_agent_signal",
                {
                    "signal_id": "mcp-complete",
                    "completion_summary": "Controlled MCP completion.",
                },
            )
            dismissed = await client.call_tool(
                "dismiss_agent_signal",
                {"signal_id": "mcp-dismiss", "reason": "Controlled dismissal."},
            )
            return listed, fetched, claimed, released, completed, dismissed

    listed, fetched, claimed, released, completed, dismissed = asyncio.run(scenario())

    assert len(listed.structured_content["signals"]) == 2
    assert fetched.structured_content["signal"]["signal_id"] == "mcp-complete"
    assert claimed.structured_content["signal"]["status"] == "claimed"
    assert released.structured_content["signal"]["status"] == "pending"
    assert completed.structured_content["signal"]["status"] == "completed"
    assert dismissed.structured_content["signal"]["status"] == "dismissed"


def test_pubsub_oidc_enforces_audience_issuer_and_service_account(monkeypatch):
    monkeypatch.setenv(
        "AGENT_SIGNAL_PUSH_AUDIENCE", "https://gateway.test/events/agent-signals"
    )
    monkeypatch.setenv(
        "AGENT_SIGNAL_PUSH_SERVICE_ACCOUNT",
        "push@test-project.iam.gserviceaccount.com",
    )
    verifier = Mock(
        return_value={
            "iss": "https://accounts.google.com",
            "email": "push@test-project.iam.gserviceaccount.com",
            "email_verified": True,
        }
    )
    monkeypatch.setattr("app.auth.pubsub_push.id_token.verify_oauth2_token", verifier)

    claims = verify_pubsub_oidc("Bearer synthetic-id-token")

    assert claims["email_verified"] is True
    assert verifier.call_args.args[2] == "https://gateway.test/events/agent-signals"

    verifier.return_value = {
        "iss": "https://accounts.google.com",
        "email": "wrong@test-project.iam.gserviceaccount.com",
        "email_verified": True,
    }
    with pytest.raises(WorkspaceAdapterError) as denied:
        verify_pubsub_oidc("Bearer synthetic-id-token")
    assert denied.value.code == "invalid_pubsub_identity"


def test_gcs_backend_uses_generation_preconditions_for_dedupe_and_updates():
    client = Mock()
    bucket = client.bucket.return_value
    blob = bucket.blob.return_value
    backend = CloudStorageAgentSignalBackend(
        bucket_name="test-state-bucket", client=client
    )
    now = datetime(2026, 8, 29, 12, tzinfo=timezone.utc)
    record = AgentSignalRecord.from_delivery(
        payload(),
        message_id="pubsub-1",
        publish_time=now,
        attributes={},
        now=now,
    )

    assert backend.create(record) is True
    backend.replace(record, generation=17)

    create_call, replace_call = blob.upload_from_string.call_args_list
    assert create_call.kwargs["if_generation_match"] == 0
    assert replace_call.kwargs["if_generation_match"] == 17
