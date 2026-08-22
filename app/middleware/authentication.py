"""Default-deny authentication middleware for Gateway capabilities."""

from __future__ import annotations

from collections.abc import Callable

from fastapi.responses import JSONResponse
from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from app.audit import correlation_id, emit_audit_record
from app.auth.gateway_auth import (
    GatewayAuthenticationConfigurationError,
    GatewayTokenAuthenticator,
)

AuthenticatorFactory = Callable[[], GatewayTokenAuthenticator]

PUBLIC_PATHS = frozenset(
    {
        "/health",
        "/docs",
        "/docs/oauth2-redirect",
        "/openapi.json",
        "/redoc",
    }
)


class GatewayAuthenticationMiddleware:
    """Protect every HTTP capability except explicitly public operational paths."""

    def __init__(
        self,
        app: ASGIApp,
        authenticator_factory: AuthenticatorFactory | None = None,
    ) -> None:
        self.app = app
        self.authenticator_factory = (
            authenticator_factory or GatewayTokenAuthenticator.from_environment
        )

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] != "http" or scope.get("path") in PUBLIC_PATHS:
            await self.app(scope, receive, send)
            return

        request = Request(scope)
        request_id = getattr(
            request.state,
            "request_id",
            correlation_id(request.headers.get("X-Correlation-ID")),
        )
        consumer = (
            "mcp_client" if request.url.path.startswith("/mcp") else "api_client"
        )

        try:
            authenticator = self.authenticator_factory()
        except GatewayAuthenticationConfigurationError:
            await self._reject(
                scope=scope,
                receive=receive,
                send=send,
                request_id=request_id,
                consumer=consumer,
                error_code="authentication_unavailable",
                status_code=503,
            )
            return

        error_code = authenticator.authenticate(request.headers.get("Authorization"))
        if error_code:
            await self._reject(
                scope=scope,
                receive=receive,
                send=send,
                request_id=request_id,
                consumer=consumer,
                error_code=error_code,
                status_code=401,
            )
            return

        emit_audit_record(
            request_id=request_id,
            action="authentication",
            resource_id=None,
            resource_type="gateway",
            result="success",
            http_status=200,
            consumer=consumer,
        )
        await self.app(scope, receive, send)

    @staticmethod
    async def _reject(
        *,
        scope: Scope,
        receive: Receive,
        send: Send,
        request_id: str,
        consumer: str,
        error_code: str,
        status_code: int,
    ) -> None:
        emit_audit_record(
            request_id=request_id,
            action="authentication",
            resource_id=None,
            resource_type="gateway",
            result="rejected" if status_code == 401 else "error",
            http_status=status_code,
            error_code=error_code,
            consumer=consumer,
        )
        response = JSONResponse(
            status_code=status_code,
            content={"error": error_code},
            headers={
                "WWW-Authenticate": "Bearer",
                "X-Correlation-ID": request_id,
            },
        )
        await response(scope, receive, send)
