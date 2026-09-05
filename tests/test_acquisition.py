import asyncio
import json

import httpx

from app import acquisition
from app.auth.principals import Principal, bind_principal, reset_principal
from app.mcp_server import mcp_server, _tool_authorization_error


def unpack(result):
    return json.loads(result.content[0].text)


def configure(monkeypatch, handler):
    monkeypatch.setenv("ACQUISITION_PORTAL_ORIGIN", "https://portal.example.test")
    monkeypatch.setenv("ACQUISITION_PORTAL_SERVICE_TOKEN", "synthetic_service_token_not_a_real_secret_123456789")
    client = httpx.AsyncClient
    monkeypatch.setattr(acquisition.httpx, "AsyncClient", lambda **kwargs: client(transport=httpx.MockTransport(handler), **kwargs))


def test_exact_truth_auth_and_no_interpretation(monkeypatch):
    truth = {"schemaVersion": "1", "items": [{"epistemicState": tag} for tag in
             ["OBSERVED_FACT", "SUPPORTED_INFERENCE", "WORKING_HYPOTHESIS", "UNKNOWN", "CONFLICT_OR_STALE"]]}
    def handler(request):
        assert request.headers["authorization"].startswith("Bearer synthetic_")
        assert "cookie" not in request.headers
        assert request.url.path == "/api/acquisition/v1/cycles/c/accounts/a"
        return httpx.Response(200, json=truth)
    configure(monkeypatch, handler)
    result = asyncio.run(mcp_server.call_tool("acquisition_get_account", {"cycle_id": "c", "account_id": "a"}))
    assert unpack(result)["data"] == truth


def test_bounds_prohibited_tools_and_scoped_principals(monkeypatch):
    def unexpected(request):
        raise AssertionError("must not call Portal")
    configure(monkeypatch, unexpected)
    for name in ["acquisition_sql", "acquisition_patch", "acquisition_set_priority", "acquisition_send", "acquisition_record_attention_disposition"]:
        assert _tool_authorization_error(Principal.management(), name) == "tool_denied"
        assert asyncio.run(mcp_server.call_tool(name, {})).is_error
    assert asyncio.run(mcp_server.call_tool("acquisition_list_cycles", {"limit": 101})).is_error
    assert asyncio.run(mcp_server.call_tool("acquisition_get_cycle", {"cycle_id": "../auth/session"})).is_error
    from dataclasses import replace
    principal = replace(Principal.management(), type="developer")
    bound = bind_principal(principal)
    try:
        assert asyncio.run(mcp_server.call_tool("acquisition_get_health", {})).is_error
        assert unpack(asyncio.run(acquisition.portal_request("GET", "/engine-health")))["httpStatus"] == 403
    finally:
        reset_principal(bound)


def test_retries_preserve_command_envelope_and_conflicts(monkeypatch):
    calls = []
    def handler(request):
        body = json.loads(request.content)
        calls.append(body)
        assert "callerId" not in body and "callerType" not in body
        assert request.headers["x-correlation-id"] == body["commandId"]
        if len(calls) == 3:
            return httpx.Response(409, json={"problem": {"code": "STALE_AGGREGATE_VERSION", "detail": "private sensitive text"}})
        return httpx.Response(202, json={"commandStatus": "accepted" if len(calls) == 1 else "already_applied", "workItemId": "same-work"})
    configure(monkeypatch, handler)
    args = {"command_id": "synthetic-command", "cycle_id": "c", "account_id": "a", "expected_lifecycle_version": 1, "reason": "synthetic"}
    first = unpack(asyncio.run(mcp_server.call_tool("acquisition_request_research", args)))
    second = unpack(asyncio.run(mcp_server.call_tool("acquisition_request_research", args)))
    assert first["data"]["workItemId"] == second["data"]["workItemId"]
    assert calls[0] == calls[1]
    conflict = unpack(asyncio.run(mcp_server.call_tool("acquisition_request_research", args)))
    assert conflict["httpStatus"] == 409
    assert conflict["data"]["code"] == "STALE_AGGREGATE_VERSION"
    assert "sensitive" not in str(conflict)


def test_unavailable_invalid_auth_and_redirects_fail_without_fallback(monkeypatch):
    for status in [401, 403, 422, 429, 500, 503, 302]:
        configure(monkeypatch, lambda request: httpx.Response(status, json={"code": "FORBIDDEN", "detail": "secret"}))
        result = asyncio.run(acquisition.portal_request("GET", "/engine-health"))
        assert result.is_error
        assert unpack(result)["httpStatus"] == status
        assert "secret" not in str(result)
        monkeypatch.undo()
    monkeypatch.delenv("ACQUISITION_PORTAL_SERVICE_TOKEN", raising=False)
    assert unpack(asyncio.run(acquisition.portal_request("GET", "/engine-health")))["httpStatus"] == 503


def test_signal_accepts_only_canonical_service_provenance():
    from tests.test_agent_signals import acquisition_dict
    from app.agent_signals import AgentSignalPayload
    import pytest
    body = acquisition_dict()
    body.update(actor_type="PANCRACIO_GATEWAY", actor_id="pancracio:gateway")
    assert AgentSignalPayload.model_validate(body).actor_type == "PANCRACIO_GATEWAY"
    body["actor_id"] = "spoof"
    with pytest.raises(ValueError):
        AgentSignalPayload.model_validate(body)


def test_buyer_dry_run_is_bounded_indirect_and_never_human_edit_or_send(monkeypatch):
    calls = []
    def handler(request):
        calls.append(request)
        if request.method == "POST":
            body = json.loads(request.content)
            assert set(body) == {"commandId", "cycleId", "accountId", "expectedAttentionVersion", "sourceKey"}
            return httpx.Response(409, json={"problemCode": "STALE_ATTENTION_VERSION"})
        return httpx.Response(200, json={"package": {"current": False, "messageability": "HOLD", "preview": {"status": "PREVIEW_ONLY", "executable": False}}})
    configure(monkeypatch, handler)
    args = {"command_id": "buyer-test", "cycle_id": "c", "account_id": "a", "expected_attention_version": 2, "source_key": "clean"}
    result = unpack(asyncio.run(mcp_server.call_tool("acquisition_request_buyer_dry_run", args)))
    assert result["httpStatus"] == 409
    assert result["data"]["code"] == "STALE_ATTENTION_VERSION"
    before = len(calls)
    for extra in [{"email": "guess@example.com"}, {"edit_text": "Human impersonation"}, {"caller_type": "HUMAN_PORTAL"}]:
        assert asyncio.run(mcp_server.call_tool("acquisition_request_buyer_dry_run", args | extra)).is_error
    assert len(calls) == before
    result = unpack(asyncio.run(mcp_server.call_tool("acquisition_get_buyer_dry_run", {"cycle_id": "c", "account_id": "a"})))
    assert result["data"]["package"]["preview"]["executable"] is False
