"""Keyless Google Workspace domain-wide delegation authentication."""

import google.auth
from google.auth import iam
from google.auth.credentials import Credentials
from google.auth.transport.requests import Request
from google.oauth2 import service_account

from app.config.settings import Settings

CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
WORKSPACE_SCOPES = (
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/documents",
    "https://www.googleapis.com/auth/spreadsheets",
)
TOKEN_URI = "https://oauth2.googleapis.com/token"


def build_delegated_credentials(settings: Settings) -> Credentials:
    """Build delegated credentials using ADC and IAM remote signing.

    No service-account key is loaded. The ADC identity calls IAM Credentials to
    sign the OAuth assertion as the configured runtime service account.
    """

    source_credentials, _ = google.auth.default(scopes=(CLOUD_PLATFORM_SCOPE,))
    signer = iam.Signer(
        Request(), source_credentials, settings.workspace_service_account_email
    )
    return service_account.Credentials(
        signer=signer,
        service_account_email=settings.workspace_service_account_email,
        token_uri=TOKEN_URI,
        scopes=WORKSPACE_SCOPES,
        subject=settings.workspace_delegated_user,
    )


def build_keyless_signing_credentials(settings: Settings) -> Credentials:
    """Build service-account credentials backed by IAM signBlob, never a key."""

    source_credentials, _ = google.auth.default(scopes=(CLOUD_PLATFORM_SCOPE,))
    signer = iam.Signer(
        Request(), source_credentials, settings.workspace_service_account_email
    )
    return service_account.Credentials(
        signer=signer,
        service_account_email=settings.workspace_service_account_email,
        token_uri=TOKEN_URI,
        scopes=(CLOUD_PLATFORM_SCOPE,),
    )
