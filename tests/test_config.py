import pytest

from app.config.settings import Settings


def test_settings_are_loaded_from_environment(monkeypatch):
    monkeypatch.setenv("WORKSPACE_DELEGATED_USER", "reader@example.com")
    monkeypatch.setenv(
        "WORKSPACE_SERVICE_ACCOUNT_EMAIL", "gateway@project.iam.gserviceaccount.com"
    )
    monkeypatch.setenv("WORKSPACE_DOC_MAX_CHARS", "12000")
    monkeypatch.setenv("WORKSPACE_SHEET_MAX_CELLS", "800")
    monkeypatch.setenv("WORKSPACE_SOURCE_MAX_DEPTH", "12")
    monkeypatch.setenv("WORKSPACE_BLOCKED_SOURCE_IDS", "blocked_123456")
    monkeypatch.setenv("WORKSPACE_AUDIT_ENABLED", "true")
    monkeypatch.setenv("WORKSPACE_SOURCE_REGISTRY_PATH", "custom/sources.yaml")
    monkeypatch.setenv("SOURCE_PROPOSAL_BUCKET", "proposal-state-bucket")
    monkeypatch.setenv("SOURCE_PROPOSAL_OBJECT", "governance/proposals.yaml")

    settings = Settings.from_environment()

    assert settings.workspace_delegated_user == "reader@example.com"
    assert settings.workspace_service_account_email == (
        "gateway@project.iam.gserviceaccount.com"
    )
    assert settings.workspace_doc_max_chars == 12000
    assert settings.workspace_sheet_max_cells == 800
    assert settings.workspace_source_max_depth == 12
    assert settings.workspace_blocked_source_ids == ("blocked_123456",)
    assert settings.workspace_audit_enabled is True
    assert settings.workspace_source_registry_path == "custom/sources.yaml"
    assert settings.source_proposal_bucket == "proposal-state-bucket"
    assert settings.source_proposal_object == "governance/proposals.yaml"


def test_missing_settings_are_rejected(monkeypatch):
    monkeypatch.delenv("WORKSPACE_DELEGATED_USER", raising=False)
    monkeypatch.delenv("WORKSPACE_SERVICE_ACCOUNT_EMAIL", raising=False)
    monkeypatch.delenv("WORKSPACE_DOC_MAX_CHARS", raising=False)
    monkeypatch.delenv("WORKSPACE_SHEET_MAX_CELLS", raising=False)
    monkeypatch.delenv("WORKSPACE_SOURCE_MAX_DEPTH", raising=False)

    with pytest.raises(ValueError, match="WORKSPACE_DELEGATED_USER"):
        Settings.from_environment()


def test_non_positive_content_limits_are_rejected(monkeypatch):
    monkeypatch.setenv("WORKSPACE_DELEGATED_USER", "reader@example.com")
    monkeypatch.setenv(
        "WORKSPACE_SERVICE_ACCOUNT_EMAIL", "gateway@project.iam.gserviceaccount.com"
    )
    monkeypatch.setenv("WORKSPACE_DOC_MAX_CHARS", "0")
    monkeypatch.setenv("WORKSPACE_SHEET_MAX_CELLS", "100")
    monkeypatch.setenv("WORKSPACE_SOURCE_MAX_DEPTH", "20")

    with pytest.raises(ValueError, match="must be positive"):
        Settings.from_environment()
