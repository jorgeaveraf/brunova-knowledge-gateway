from fastapi.testclient import TestClient

from app.adapters.google_workspace.models import DriveFile
from app.main import app, get_workspace_adapter


class FakeWorkspaceAdapter:
    delegated_user = "reader@example.com"

    def check_connection(self):
        return None

    def list_files(self, *, limit):
        assert limit == 2
        return [DriveFile(name="Roadmap", type="document")]


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
    assert response.json() == {"files": [{"name": "Roadmap", "type": "document"}]}


def test_drive_list_rejects_unbounded_request():
    app.dependency_overrides[get_workspace_adapter] = FakeWorkspaceAdapter
    try:
        response = TestClient(app).get("/workspace/drive/list?limit=101")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422
    assert response.json() == {"detail": "limit must be at most 100"}
