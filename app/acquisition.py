"""Bounded Portal consumer. No database, business interpretation or local truth."""

from __future__ import annotations

import json
import os
import re
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

import httpx
from mcp.types import CallToolResult, TextContent
from pydantic import Field

from app.audit import correlation_id, emit_audit_record
from app.auth.principals import active_principal

Identifier = Annotated[str, Field(min_length=1, max_length=200, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")]
PageLimit = Annotated[int, Field(ge=1, le=100)]
Cursor = Annotated[str, Field(min_length=1, max_length=256)]
TOOLS = frozenset({
    "acquisition_list_cycles", "acquisition_get_cycle", "acquisition_list_accounts",
    "acquisition_get_account", "acquisition_list_priorities", "acquisition_list_attention",
    "acquisition_get_attention", "acquisition_get_attention_counts", "acquisition_list_work",
    "acquisition_get_health", "acquisition_request_research", "acquisition_get_buyer_dry_run", "acquisition_request_buyer_dry_run",
    "acquisition_record_attention_disposition", "acquisition_authorize_controlled_effect",
    "acquisition_request_effect_reconciliation", "acquisition_acknowledge_effect_attention", "acquisition_edit_message_draft",
    "acquisition_get_effect", "acquisition_get_effect_bindings", "acquisition_list_effect_attention",
    "acquisition_list_crm", "acquisition_request_pre_outbound_sync", "acquisition_request_crm_reconciliation",
    "acquisition_review_commercial_handoff", "acquisition_accept_commercial_handoff",
})


async def portal_request(method: str, path: str, *, params: dict | None = None,
                         body: dict | None = None) -> CallToolResult:
    """Called only by fixed tools; never accepts caller URLs, headers or identity."""
    principal = active_principal()
    cid = correlation_id(body.get("commandId") if body else None)
    status = 503
    code = "ACQUISITION_UNAVAILABLE"
    payload: Any = {"code": code}
    if principal.type != "management":
        status, code = 403, "ACQUISITION_CAPABILITY_DENIED"
        payload = {"code": code}
    else:
        base = os.getenv("ACQUISITION_PORTAL_ORIGIN", "").strip()
        secret = os.getenv("ACQUISITION_PORTAL_SERVICE_TOKEN", "").strip()
        origin = urlsplit(base)
        configured = (origin.scheme == "https" and bool(origin.hostname)
                      and not origin.username and not origin.password
                      and origin.path in ("", "/") and not origin.query and not origin.fragment
                      and bool(re.fullmatch(r"[A-Za-z0-9_-]{43,128}", secret)))
        if configured:
            try:
                async with httpx.AsyncClient(timeout=15, follow_redirects=False, trust_env=False) as client:
                    async with client.stream(method, base.rstrip("/") + "/api/acquisition/v1" + path,
                                             params={k: v for k, v in (params or {}).items() if v is not None},
                                             json=body, headers={"Authorization": "Bearer " + secret,
                                                                 "x-correlation-id": cid}) as response:
                        raw = bytearray()
                        async for chunk in response.aiter_bytes():
                            raw.extend(chunk)
                            if len(raw) > 2 * 1024 * 1024:
                                raise ValueError("bounded_response_exceeded")
                        data = json.loads(raw)
                        if not isinstance(data, dict):
                            raise ValueError("invalid_contract")
                        status = response.status_code
                        if 200 <= status < 300:
                            payload, code = data, None
                        else:
                            candidate = data.get("code") or data.get("problemCode") or (data.get("problem") or {}).get("code")
                            code = candidate if isinstance(candidate, str) and re.fullmatch(r"[A-Z_]{1,80}", candidate) else "ACQUISITION_REQUEST_FAILED"
                            # Error bodies are not trusted content. Preserve status/code,
                            # never expose upstream text, credentials or stack traces.
                            payload = {"code": code, "correlationId": cid}
            except (httpx.HTTPError, ValueError, TypeError):
                status, code = 503, "ACQUISITION_UNAVAILABLE"
                payload = {"code": code, "correlationId": cid}
    emit_audit_record(request_id=cid, action="acquisition_control", resource_id=None,
                      resource_type="engine_control", result="success" if status < 300 else "rejected",
                      http_status=status, error_code=code, provider="acquisition")
    return CallToolResult(content=[TextContent(type="text", text=json.dumps({"httpStatus": status, "data": payload}))],
                          is_error=not 200 <= status < 300)


def register_acquisition_tools(server: Any) -> None:
    async def management(operation, command_id, objective_reference, request):
        return await portal_request("POST", "/management-actions/" + operation, body={
            "commandId": command_id, "objectiveReference": objective_reference, "request": request})

    @server.tool()
    async def acquisition_list_crm(cycle_id: Identifier, account_id: Identifier | None = None,
            status: Literal["READY", "IN_PROGRESS", "SYNCED", "RECONCILIATION_REQUIRED", "BLOCKED"] | None = None,
            limit: Annotated[int, Field(ge=1, le=50)] = 20, after: Identifier | None = None) -> CallToolResult:
        """Read authoritative bounded CRM mappings, association observations and handoff/authority transfer. Engine state is not a mirror of HubSpot commercial activity. Resolve Account IDs through bounded Account lookup; never query HubSpot directly."""
        return await portal_request("GET", f"/cycles/{cycle_id}/crm", params={"accountId": account_id, "status": status, "limit": limit, "after": after})

    @server.tool()
    async def acquisition_request_pre_outbound_sync(command_id: Identifier, objective_reference: Identifier,
            cycle_id: Identifier, account_id: Identifier) -> CallToolResult:
        """Request exact CRM boundary admission under an admitted Management objective. Engine derives all readiness from PostgreSQL. May enqueue controlled synthetic Company/Contact work; no Deal, outreach or commercial activation. No caller-supplied eligibility or provider IDs."""
        return await management("REQUEST_PRE_OUTBOUND_SYNC", command_id, objective_reference, {"cycleId": cycle_id, "accountId": account_id})

    @server.tool()
    async def acquisition_request_crm_reconciliation(command_id: Identifier, objective_reference: Identifier,
            intent_id: Identifier, expected_version: Annotated[int, Field(ge=1)]) -> CallToolResult:
        """Request CRM reconciliation under exact admitted objective/version. Lookup first; uncertain creates cannot be blindly repeated. No mapping reassignment or generic property patch."""
        return await management("REQUEST_CRM_RECONCILIATION", command_id, objective_reference, {"intentId": intent_id, "expectedVersion": expected_version})

    @server.tool()
    async def acquisition_review_commercial_handoff(command_id: Identifier, objective_reference: Identifier,
            handoff_id: Identifier, decision: Literal["RECOMMEND", "DECLINE"],
            reason: Annotated[str, Field(min_length=1, max_length=1000)]) -> CallToolResult:
        """Management review of QUESTION/UNKNOWN under admitted objective. Recommendation is not acceptance, opportunity or Deal creation. Preserve actual Agent provenance."""
        return await management("REVIEW_COMMERCIAL_HANDOFF", command_id, objective_reference, {"handoffId": handoff_id, "decision": decision, "reason": reason})

    @server.tool()
    async def acquisition_accept_commercial_handoff(command_id: Identifier, objective_reference: Identifier,
            handoff_id: Identifier, reason: Annotated[str, Field(min_length=1, max_length=1000)]) -> CallToolResult:
        """Accept a supported handoff under an admitted objective. Transfers Human commercial follow-up authority to HubSpot and blocks autonomous pre-Human progression. Never creates a Deal or impersonates Human."""
        return await management("ACCEPT_COMMERCIAL_HANDOFF", command_id, objective_reference, {"handoffId": handoff_id, "reason": reason})

    @server.tool()
    async def acquisition_record_attention_disposition(command_id: Identifier, objective_reference: Identifier,
            attention_id: Identifier, expected_version: Annotated[int, Field(ge=1)],
            disposition: Literal["CONTINUE", "HOLD", "REJECT"], reason: Annotated[str, Field(min_length=1,max_length=1000)],
            notes: Annotated[str, Field(max_length=2000)] = "") -> CallToolResult:
        """Management decision under an admitted Human objective, not token-only autonomy. Preserve PANCRACIO_GATEWAY provenance, exact version and retry identity. No gate activation."""
        return await management("RECORD_ATTENTION_DISPOSITION", command_id, objective_reference, {
            "attentionId": attention_id, "expectedVersion": expected_version, "disposition": disposition, "reason": reason, "notes": notes})

    @server.tool()
    async def acquisition_authorize_controlled_effect(command_id: Identifier, objective_reference: Identifier,
            message_id: Identifier, target_id: Identifier, sender_id: Identifier,
            expected_binding_hash: Annotated[str, Field(pattern=r"^[a-f0-9]{64}$")],
            expires_at: Annotated[str, Field(min_length=20,max_length=40)]) -> CallToolResult:
        """Authorize only an exact controlled effect covered by an admitted objective and current policy. This may enqueue real controlled work; never invoke without applicable Human authority. Commercial transport remains unapproved. Not a send or transport tool."""
        return await management("AUTHORIZE_EFFECT", command_id, objective_reference, {
            "messageId": message_id, "targetId": target_id, "senderId": sender_id,
            "expectedBindingHash": expected_binding_hash, "expiresAt": expires_at})

    @server.tool()
    async def acquisition_request_effect_reconciliation(command_id: Identifier, objective_reference: Identifier, intent_id: Identifier) -> CallToolResult:
        """Request bounded lookup-only reconciliation under an admitted objective. Never resend UNKNOWN work or reset total attempts."""
        return await management("REQUEST_EFFECT_RECONCILIATION", command_id, objective_reference, {"intentId": intent_id})

    @server.tool()
    async def acquisition_acknowledge_effect_attention(command_id: Identifier, objective_reference: Identifier,
            attention_id: Identifier, expected_version: Annotated[int, Field(ge=1)],
            reason: Annotated[str, Field(min_length=1,max_length=1000)]) -> CallToolResult:
        """Acknowledge bounded Management Attention under an admitted objective. Does not send, approve commercial outreach or hand off CRM ownership."""
        return await management("ACKNOWLEDGE_EFFECT_ATTENTION", command_id, objective_reference, {
            "attentionId": attention_id, "expectedVersion": expected_version, "reason": reason})

    @server.tool()
    async def acquisition_edit_message_draft(command_id: Identifier, objective_reference: Identifier,
            cycle_id: Identifier, account_id: Identifier, expected_attention_version: Annotated[int, Field(ge=1)],
            source_key: Literal["clean", "unresolved", "claim-trap", "suppressed", "ambiguous", "stale", "guessed"],
            edit_text: Annotated[str, Field(min_length=1,max_length=4000)]) -> CallToolResult:
        """Create a new synthetic draft version under an admitted objective. Claims are revalidated; an edit is never approval or send authority."""
        return await management("EDIT_MESSAGE_DRAFT", command_id, objective_reference, {
            "cycleId": cycle_id, "accountId": account_id, "expectedAttentionVersion": expected_attention_version,
            "sourceKey": source_key, "editText": edit_text})

    @server.tool()
    async def acquisition_get_effect(intent_id: Identifier) -> CallToolResult:
        """Read bounded authoritative effect status, exact authorization and observations; do not infer authorization from readiness."""
        return await portal_request("GET", f"/effects/{intent_id}")

    @server.tool()
    async def acquisition_get_effect_bindings(message_id: Identifier) -> CallToolResult:
        """Read exact current controlled bindings (maximum 25). Eligible is not permission to execute."""
        return await portal_request("GET", f"/messages/{message_id}/controlled-test-bindings")

    @server.tool()
    async def acquisition_list_effect_attention(cycle_id: Identifier) -> CallToolResult:
        """Read shared-capacity effect Attention (maximum 25), including items waiting for capacity."""
        return await portal_request("GET", f"/cycles/{cycle_id}/effect-attention")

    @server.tool()
    async def acquisition_get_buyer_dry_run(cycle_id: Identifier, account_id: Identifier) -> CallToolResult:
        """Read Engine buyer/contact evidence, messageability, claims and last ten immutable drafts. PREVIEW_ONLY is never send authority; stale packages are not READY."""
        return await portal_request("GET", f"/cycles/{cycle_id}/accounts/{account_id}/buyer-dry-run")

    @server.tool()
    async def acquisition_request_buyer_dry_run(command_id: Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")],
                                               cycle_id: Identifier, account_id: Identifier,
                                               expected_attention_version: Annotated[int, Field(ge=1)],
                                               source_key: Literal["clean", "unresolved", "claim-trap", "suppressed", "ambiguous", "stale", "guessed"]) -> CallToolResult:
        """Request synthetic manual-source Buyer/Message dry run only after authorized CONTINUE/current priority. No arbitrary person/email or send. Edits use the separate objective-governed Management tool. Retry exact command ID; acceptance is not completion."""
        return await portal_request("POST", "/commands/request-buyer-dry-run", body={
            "commandId": command_id, "cycleId": cycle_id, "accountId": account_id,
            "expectedAttentionVersion": expected_attention_version, "sourceKey": source_key,
        })

    @server.tool()
    async def acquisition_list_cycles(status: Literal["DESIGN", "CALIBRATION", "ACTIVE", "REVIEW", "CLOSED"] | None = None,
                                      limit: PageLimit = 25, cursor: Cursor | None = None) -> CallToolResult:
        """Resolve Cycle IDs/state/counts from bounded authoritative pages."""
        return await portal_request("GET", "/cycles", params=locals())

    @server.tool()
    async def acquisition_get_cycle(cycle_id: Identifier) -> CallToolResult:
        """Read the exact Engine Cycle summary."""
        return await portal_request("GET", f"/cycles/{cycle_id}")

    @server.tool()
    async def acquisition_list_accounts(cycle_id: Identifier, limit: PageLimit = 25, cursor: Cursor | None = None,
                                        stage: Literal["DISCOVERED", "RESEARCHING", "RESEARCH_DECIDED"] | None = None,
                                        outcome: Literal["QUALIFIED", "HOLD", "REJECTED"] | None = None) -> CallToolResult:
        """Resolve Account IDs by bounded summaries. Follow cursors; no unbounded dump."""
        return await portal_request("GET", f"/cycles/{cycle_id}/accounts",
                                    params={"limit": limit, "cursor": cursor, "stage": stage, "outcome": outcome})

    @server.tool()
    async def acquisition_get_account(cycle_id: Identifier, account_id: Identifier) -> CallToolResult:
        """Preserve OBSERVED_FACT, SUPPORTED_INFERENCE, WORKING_HYPOTHESIS, UNKNOWN and CONFLICT_OR_STALE separately; do not flatten epistemic truth."""
        return await portal_request("GET", f"/cycles/{cycle_id}/accounts/{account_id}")

    @server.tool()
    async def acquisition_list_priorities(cycle_id: Identifier) -> CallToolResult:
        """Engine ordered top 50; preserve tier, why-now, factors, limits and unknowns verbatim."""
        return await portal_request("GET", f"/cycles/{cycle_id}/priorities")

    @server.tool()
    async def acquisition_list_attention(cycle_id: Identifier, active_only: bool = True) -> CallToolResult:
        """Engine ordered bounded Attention (max 50); active_only selects active capacity."""
        return await portal_request("GET", f"/cycles/{cycle_id}/attention", params={"active": str(active_only).lower()})

    @server.tool()
    async def acquisition_get_attention(attention_id: Identifier) -> CallToolResult:
        """Read Attention rationale/version and Human disposition without changing it."""
        return await portal_request("GET", f"/attention/{attention_id}")

    @server.tool()
    async def acquisition_get_attention_counts(cycle_id: Identifier) -> CallToolResult:
        """Read authoritative active/overflow semantics and configured capacity."""
        return await portal_request("GET", f"/cycles/{cycle_id}/attention-counts")

    @server.tool()
    async def acquisition_list_work(cycle_id: Identifier | None = None, limit: PageLimit = 25, cursor: Cursor | None = None,
                                    state: Literal["QUEUED", "WORKING", "COMPLETED", "BLOCKED", "FAILED", "CANCELLED"] | None = None) -> CallToolResult:
        """Bounded authoritative work status, not permission to complete or mutate work."""
        return await portal_request("GET", "/work", params={"cycleId": cycle_id, "limit": limit, "cursor": cursor, "state": state})

    @server.tool()
    async def acquisition_get_health() -> CallToolResult:
        """Read Engine/PostgreSQL health, pending work and safety gates; no activation."""
        return await portal_request("GET", "/engine-health")

    @server.tool()
    async def acquisition_request_research(command_id: Annotated[str, Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9][A-Za-z0-9._:-]*$")],
                                            cycle_id: Identifier, account_id: Identifier,
                                            expected_lifecycle_version: Annotated[int, Field(ge=1)],
                                            reason: Annotated[str, Field(min_length=1, max_length=1000)]) -> CallToolResult:
        """Request existing Engine research only when authorized. Reuse the exact command ID/payload for uncertain retries; conflict requires reading current truth. Acceptance is not completion. Management dispositions use their separate governed tool."""
        return await portal_request("POST", "/commands/request-account-research", body={
            "schemaVersion": "1", "commandId": command_id, "cycleId": cycle_id, "accountId": account_id,
            "expectedLifecycleVersion": expected_lifecycle_version, "reason": reason,
        })
