from google.auth.exceptions import DefaultCredentialsError, RefreshError
from googleapiclient.errors import HttpError
from unittest.mock import Mock

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


def test_missing_workspace_resource_maps_to_404():
    response = Mock(status=404, reason="Not Found")
    error = map_google_error(HttpError(response, b'{"error":{"message":"missing"}}'))

    assert error.code == "resource_not_found"
    assert error.status_code == 404


def test_google_rejected_range_maps_to_422():
    response = Mock(status=400, reason="Bad Request")
    error = map_google_error(HttpError(response, b'{"error":{"message":"bad range"}}'))

    assert error.code == "invalid_workspace_request"
    assert error.status_code == 422
