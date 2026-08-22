"""Environment-backed, non-secret application configuration."""

import os
from dataclasses import dataclass
from functools import lru_cache


@dataclass(frozen=True)
class Settings:
    workspace_delegated_user: str
    workspace_service_account_email: str

    @classmethod
    def from_environment(cls) -> "Settings":
        delegated_user = os.getenv("WORKSPACE_DELEGATED_USER", "").strip()
        service_account = os.getenv("WORKSPACE_SERVICE_ACCOUNT_EMAIL", "").strip()

        missing = [
            name
            for name, value in (
                ("WORKSPACE_DELEGATED_USER", delegated_user),
                ("WORKSPACE_SERVICE_ACCOUNT_EMAIL", service_account),
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
        return cls(delegated_user, service_account)


@lru_cache
def get_settings() -> Settings:
    return Settings.from_environment()
