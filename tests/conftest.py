"""Shared test configuration with ephemeral, non-production credentials."""

import secrets
from dataclasses import dataclass

import pytest


@dataclass(frozen=True, repr=False)
class EphemeralGatewayToken:
    value: str

    def __str__(self) -> str:
        return self.value

    def __repr__(self) -> str:
        return "<ephemeral-gateway-token>"


@pytest.fixture(autouse=True)
def ephemeral_gateway_token(monkeypatch) -> EphemeralGatewayToken:
    token = EphemeralGatewayToken(secrets.token_urlsafe(32))
    monkeypatch.setenv("BRUNOVA_GATEWAY_TOKEN", token.value)
    return token
