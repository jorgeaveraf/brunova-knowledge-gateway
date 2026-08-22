"""Stable, non-sensitive errors for Google Workspace calls."""

from google.auth.exceptions import DefaultCredentialsError, GoogleAuthError, RefreshError
from googleapiclient.errors import HttpError


class WorkspaceAdapterError(Exception):
    def __init__(self, code: str, message: str, status_code: int) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


def map_google_error(error: Exception) -> WorkspaceAdapterError:
    text = str(error).lower()

    if isinstance(error, DefaultCredentialsError):
        return WorkspaceAdapterError(
            "credentials_unavailable",
            "Application Default Credentials are not available to the runtime.",
            503,
        )

    if isinstance(error, RefreshError):
        if "unauthorized_client" in text or "access_denied" in text:
            return WorkspaceAdapterError(
                "domain_wide_delegation_invalid",
                "Domain Wide Delegation is missing or does not authorize the required Workspace scopes.",
                403,
            )
        if "invalid_grant" in text or "invalid email" in text or "user id" in text:
            return WorkspaceAdapterError(
                "delegated_user_invalid",
                "The configured delegated Workspace user is invalid or cannot be impersonated.",
                403,
            )
        if "iam.serviceaccountcredentials" in text or "signblob" in text:
            return WorkspaceAdapterError(
                "iam_signing_denied",
                "The runtime identity cannot sign delegated credentials. Grant Service Account Token Creator on the runtime service account.",
                403,
            )
        return WorkspaceAdapterError(
            "authentication_failed",
            "Google Workspace authentication failed. Check runtime identity and delegation configuration.",
            503,
        )

    if isinstance(error, GoogleAuthError):
        if "iam.serviceaccountcredentials" in text or "signblob" in text:
            return WorkspaceAdapterError(
                "iam_signing_denied",
                "The runtime identity cannot sign delegated credentials. Grant Service Account Token Creator on the runtime service account.",
                403,
            )
        return WorkspaceAdapterError(
            "authentication_failed",
            "Google Workspace authentication failed. Check runtime identity and delegation configuration.",
            503,
        )

    if isinstance(error, HttpError):
        status = getattr(error.resp, "status", None)
        if status == 403 and any(
            marker in text
            for marker in ("accessnotconfigured", "servicedisabled", "api has not been used")
        ):
            return WorkspaceAdapterError(
                "api_not_enabled",
                "The required Google API is not enabled in the GCP project.",
                503,
            )
        if status in (401, 403):
            return WorkspaceAdapterError(
                "insufficient_permissions",
                "The delegated identity does not have permission to perform this Workspace operation.",
                403,
            )
        if status == 404:
            return WorkspaceAdapterError(
                "resource_not_found",
                "The requested Workspace resource was not found or is not visible to the delegated user.",
                404,
            )
        if status == 400:
            return WorkspaceAdapterError(
                "invalid_workspace_request",
                "Google Workspace rejected the resource identifier or range.",
                422,
            )
        if status == 429:
            return WorkspaceAdapterError(
                "workspace_rate_limited",
                "Google Workspace temporarily rate limited the request.",
                503,
            )

    return WorkspaceAdapterError(
        "workspace_api_error",
        "Google Workspace returned an unexpected error while processing the request.",
        502,
    )
