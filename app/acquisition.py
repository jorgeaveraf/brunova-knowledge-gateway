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
    "acquisition_get_health", "acquisition_request_research",
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
                            candidate = data.get("code") or (data.get("problem") or {}).get("code")
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
        """Request existing Engine research only when authorized. Reuse the exact command ID/payload for uncertain retries; conflict requires reading current truth. Acceptance is not completion. No Human Attention disposition authority."""
        return await portal_request("POST", "/commands/request-account-research", body={
            "schemaVersion": "1", "commandId": command_id, "cycleId": cycle_id, "accountId": account_id,
            "expectedLifecycleVersion": expected_lifecycle_version, "reason": reason,
        })
