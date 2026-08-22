import pytest

from app.config.settings import Settings


def test_settings_are_loaded_from_environment(monkeypatch):
    monkeypatch.setenv("WORKSPACE_DELEGATED_USER", "reader@example.com")
    monkeypatch.setenv(
        "WORKSPACE_SERVICE_ACCOUNT_EMAIL", "gateway@project.iam.gserviceaccount.com"
    )

    settings = Settings.from_environment()

    assert settings.workspace_delegated_user == "reader@example.com"
    assert settings.workspace_service_account_email == (
        "gateway@project.iam.gserviceaccount.com"
    )


def test_missing_settings_are_rejected(monkeypatch):
    monkeypatch.delenv("WORKSPACE_DELEGATED_USER", raising=False)
    monkeypatch.delenv("WORKSPACE_SERVICE_ACCOUNT_EMAIL", raising=False)

    with pytest.raises(ValueError, match="WORKSPACE_DELEGATED_USER"):
        Settings.from_environment()
