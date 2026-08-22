from unittest.mock import Mock, patch

from app.adapters.google_workspace.auth import (
    CLOUD_PLATFORM_SCOPE,
    WORKSPACE_SCOPES,
    build_delegated_credentials,
)
from app.config.settings import Settings


def test_build_delegated_credentials_uses_adc_remote_signer_and_subject():
    settings = Settings(
        workspace_delegated_user="reader@example.com",
        workspace_service_account_email="gateway@project.iam.gserviceaccount.com",
        workspace_doc_max_chars=10000,
        workspace_sheet_max_cells=1000,
        workspace_allowed_shared_drive_ids=(),
        workspace_allowed_folder_ids=("allowed_folder_123",),
        workspace_blocked_source_ids=(),
        workspace_source_max_depth=20,
        workspace_audit_enabled=True,
    )
    source_credentials = Mock()
    signer = Mock()

    with (
        patch(
            "app.adapters.google_workspace.auth.google.auth.default",
            return_value=(source_credentials, "project"),
        ) as default,
        patch(
            "app.adapters.google_workspace.auth.iam.Signer", return_value=signer
        ) as signer_class,
    ):
        delegated = build_delegated_credentials(settings)

    default.assert_called_once_with(scopes=(CLOUD_PLATFORM_SCOPE,))
    signer_class.assert_called_once()
    assert signer_class.call_args.args[1:] == (
        source_credentials,
        settings.workspace_service_account_email,
    )
    assert delegated.service_account_email == settings.workspace_service_account_email
    assert delegated._subject == settings.workspace_delegated_user
    assert delegated.scopes == WORKSPACE_SCOPES
