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

    @classmethod
    def from_environment(cls) -> "Settings":
        delegated_user = os.getenv("WORKSPACE_DELEGATED_USER", "").strip()
        service_account = os.getenv("WORKSPACE_SERVICE_ACCOUNT_EMAIL", "").strip()
        doc_max_chars = os.getenv("WORKSPACE_DOC_MAX_CHARS", "").strip()
        sheet_max_cells = os.getenv("WORKSPACE_SHEET_MAX_CELLS", "").strip()

        missing = [
            name
            for name, value in (
                ("WORKSPACE_DELEGATED_USER", delegated_user),
                ("WORKSPACE_SERVICE_ACCOUNT_EMAIL", service_account),
                ("WORKSPACE_DOC_MAX_CHARS", doc_max_chars),
                ("WORKSPACE_SHEET_MAX_CELLS", sheet_max_cells),
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
        except ValueError as error:
            raise ValueError("Workspace content limits must be integers") from error
        if parsed_doc_max_chars < 1 or parsed_sheet_max_cells < 1:
            raise ValueError("Workspace content limits must be positive")
        return cls(
            delegated_user,
            service_account,
            parsed_doc_max_chars,
            parsed_sheet_max_cells,
        )


@lru_cache
def get_settings() -> Settings:
    return Settings.from_environment()
