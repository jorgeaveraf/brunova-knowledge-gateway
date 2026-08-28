"""Environment-backed, non-secret application configuration."""

import os
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class Settings:
    workspace_delegated_user: str
    workspace_service_account_email: str
    workspace_doc_max_chars: int
    workspace_sheet_max_cells: int
    workspace_blocked_source_ids: tuple[str, ...]
    workspace_source_max_depth: int
    workspace_audit_enabled: bool
    workspace_source_registry_path: str
    source_proposal_bucket: str = ""
    source_proposal_object: str = "source_proposals.yaml"
    hubspot_mcp_client_id: str = ""
    hubspot_mcp_app_id: str = ""
    hubspot_mcp_server_url: str = "https://mcp.hubspot.com"
    hubspot_mcp_redirect_uri: str = ""
    hubspot_oauth_state_bucket: str = ""
    hubspot_oauth_state_prefix: str = "oauth/hubspot"
    hubspot_oauth_state_ttl_seconds: int = 600
    n8n_discovery_ttl_seconds: int = 60
    n8n_timeout_seconds: int = 30
    openwa_discovery_ttl_seconds: int = 60
    openwa_timeout_seconds: int = 30

    @classmethod
    def from_environment(cls) -> "Settings":
        delegated_user = os.getenv("WORKSPACE_DELEGATED_USER", "").strip()
        service_account = os.getenv("WORKSPACE_SERVICE_ACCOUNT_EMAIL", "").strip()
        doc_max_chars = os.getenv("WORKSPACE_DOC_MAX_CHARS", "").strip()
        sheet_max_cells = os.getenv("WORKSPACE_SHEET_MAX_CELLS", "").strip()
        source_max_depth = os.getenv("WORKSPACE_SOURCE_MAX_DEPTH", "").strip()

        missing = [
            name
            for name, value in (
                ("WORKSPACE_DELEGATED_USER", delegated_user),
                ("WORKSPACE_SERVICE_ACCOUNT_EMAIL", service_account),
                ("WORKSPACE_DOC_MAX_CHARS", doc_max_chars),
                ("WORKSPACE_SHEET_MAX_CELLS", sheet_max_cells),
                ("WORKSPACE_SOURCE_MAX_DEPTH", source_max_depth),
            )
            if not value
        ]
        if missing:
            raise ValueError(f"Missing required configuration: {', '.join(missing)}")
        if "@" not in delegated_user:
            raise ValueError("WORKSPACE_DELEGATED_USER must be an email address")
        if not service_account.endswith(".iam.gserviceaccount.com"):
            raise ValueError(
                "WORKSPACE_SERVICE_ACCOUNT_EMAIL must be a service account email"
            )
        try:
            parsed_doc_max_chars = int(doc_max_chars)
            parsed_sheet_max_cells = int(sheet_max_cells)
            parsed_source_max_depth = int(source_max_depth)
        except ValueError as error:
            raise ValueError("Workspace content limits must be integers") from error
        if min(
            parsed_doc_max_chars,
            parsed_sheet_max_cells,
            parsed_source_max_depth,
        ) < 1:
            raise ValueError("Workspace limits must be positive")
        return cls(
            delegated_user,
            service_account,
            parsed_doc_max_chars,
            parsed_sheet_max_cells,
            _csv_ids("WORKSPACE_BLOCKED_SOURCE_IDS"),
            parsed_source_max_depth,
            _environment_bool("WORKSPACE_AUDIT_ENABLED", default=True),
            os.getenv("WORKSPACE_SOURCE_REGISTRY_PATH", "").strip()
            or "app/config/sources.yaml",
            os.getenv("SOURCE_PROPOSAL_BUCKET", "").strip(),
            os.getenv("SOURCE_PROPOSAL_OBJECT", "").strip()
            or "source_proposals.yaml",
            os.getenv("HUBSPOT_MCP_CLIENT_ID", "").strip(),
            os.getenv("HUBSPOT_MCP_APP_ID", "").strip(),
            os.getenv("HUBSPOT_MCP_SERVER_URL", "").strip()
            or "https://mcp.hubspot.com",
            os.getenv("HUBSPOT_MCP_REDIRECT_URI", "").strip(),
            os.getenv("HUBSPOT_OAUTH_STATE_BUCKET", "").strip(),
            os.getenv("HUBSPOT_OAUTH_STATE_PREFIX", "").strip()
            or "oauth/hubspot",
            _positive_environment_int("HUBSPOT_OAUTH_STATE_TTL_SECONDS", 600),
            _positive_environment_int("N8N_DISCOVERY_TTL_SECONDS", 60),
            _positive_environment_int("N8N_TIMEOUT_SECONDS", 30),
            _positive_environment_int("OPENWA_DISCOVERY_TTL_SECONDS", 60),
            _positive_environment_int("OPENWA_TIMEOUT_SECONDS", 30),
        )


@lru_cache
def get_settings() -> Settings:
    return Settings.from_environment()


def _csv_ids(name: str) -> tuple[str, ...]:
    return tuple(
        value.strip()
        for value in os.getenv(name, "").split(",")
        if value.strip()
    )


def _environment_bool(name: str, *, default: bool) -> bool:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    value = raw_value.strip().lower()
    if value in ("1", "true", "yes", "on"):
        return True
    if value in ("0", "false", "no", "off"):
        return False
    raise ValueError(f"{name} must be true or false")


def _positive_environment_int(name: str, default: int) -> int:
    raw_value = os.getenv(name, "").strip()
    try:
        value = int(raw_value) if raw_value else default
    except ValueError as error:
        raise ValueError(f"{name} must be an integer") from error
    if value < 1:
        raise ValueError(f"{name} must be positive")
    return value
