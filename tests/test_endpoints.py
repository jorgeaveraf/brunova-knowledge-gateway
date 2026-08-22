from fastapi.testclient import TestClient

from app.adapters.google_workspace.models import (
    DriveFile,
    GoogleDocContent,
    SheetRangeContent,
)
from app.main import app, get_docs_adapter, get_sheets_adapter, get_workspace_adapter


class FakeWorkspaceAdapter:
    delegated_user = "reader@example.com"

    def check_connection(self):
        return None

    def list_files(self, *, limit):
        assert limit == 2
        return [DriveFile(id="document_12345", name="Roadmap", type="document")]


class FakeDocsAdapter:
    max_chars = 20

    def get_document(self, document_id, *, max_chars):
        assert document_id == "document_12345"
        assert max_chars == 20
        return GoogleDocContent(
            id=document_id,
            name="Controlled document",
            mime_type="application/vnd.google-apps.document",
            modified_time="2026-08-22T00:00:00Z",
            text="controlled content",
            truncated=False,
            limit=max_chars,
        )


class FakeSheetsAdapter:
    max_cells = 100

    def get_range(self, spreadsheet_id, *, range_name):
        assert spreadsheet_id == "spreadsheet_12345"
        assert range_name == "A1:F10"
        return SheetRangeContent(
            spreadsheet_id=spreadsheet_id,
            range=range_name,
            values=[["Header"], ["Value"]],
        )


def test_workspace_status_response():
    app.dependency_overrides[get_workspace_adapter] = FakeWorkspaceAdapter
    try:
        response = TestClient(app).get("/workspace/status")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "service": "brunova-knowledge-gateway",
        "workspace": {"connected": True, "delegated_user": "reader@example.com"},
    }


def test_drive_list_response():
    app.dependency_overrides[get_workspace_adapter] = FakeWorkspaceAdapter
    try:
        response = TestClient(app).get("/workspace/drive/list?limit=2")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "files": [
            {"id": "document_12345", "name": "Roadmap", "type": "document"}
        ]
    }


def test_drive_list_rejects_unbounded_request():
    app.dependency_overrides[get_workspace_adapter] = FakeWorkspaceAdapter
    try:
        response = TestClient(app).get("/workspace/drive/list?limit=101")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422
    assert response.json() == {"detail": "limit must be at most 100"}


def test_document_content_response():
    app.dependency_overrides[get_docs_adapter] = FakeDocsAdapter
    try:
        response = TestClient(app).get("/workspace/docs/document_12345")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["text"] == "controlled content"
    assert response.json()["limit"] == 20
    assert response.json()["truncated"] is False


def test_sheet_range_response():
    app.dependency_overrides[get_sheets_adapter] = FakeSheetsAdapter
    try:
        response = TestClient(app).get(
            "/workspace/sheets/spreadsheet_12345?range=A1:F10"
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "spreadsheet_id": "spreadsheet_12345",
        "range": "A1:F10",
        "values": [["Header"], ["Value"]],
    }


def test_sheet_range_is_required():
    app.dependency_overrides[get_sheets_adapter] = FakeSheetsAdapter
    try:
        response = TestClient(app).get("/workspace/sheets/spreadsheet_12345")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422
