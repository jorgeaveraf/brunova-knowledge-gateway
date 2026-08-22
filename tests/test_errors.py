from google.auth.exceptions import DefaultCredentialsError, RefreshError

from app.adapters.google_workspace.errors import map_google_error


def test_domain_wide_delegation_error_is_actionable_and_safe():
    error = map_google_error(
        RefreshError("unauthorized_client: client is unauthorized for scopes")
    )

    assert error.code == "domain_wide_delegation_invalid"
    assert error.status_code == 403
    assert "Domain Wide Delegation" in error.message
    assert "unauthorized_client" not in error.message


def test_missing_adc_error_is_actionable():
    error = map_google_error(DefaultCredentialsError("metadata unavailable"))

    assert error.code == "credentials_unavailable"
    assert error.status_code == 503
