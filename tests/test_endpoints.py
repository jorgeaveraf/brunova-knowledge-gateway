from fastapi.testclient import TestClient

from app.adapters.google_workspace.models import (
    DriveFile,
    GoogleDocContent,
    SheetRangeContent,
    WorkspaceResource,
)
from app.adapters.google_workspace.errors import WorkspaceAdapterError
from app.main import (
    app,
    get_docs_adapter,
    get_sheets_adapter,
    get_source_policy,
    get_workspace_adapter,
)


class FakeWorkspaceAdapter:
    delegated_user = "reader@example.com"

    def check_connection(self):
        return None

    def list_files(self, *, limit, source_policy):
        assert limit == 2
        return [DriveFile(id="document_12345", name="Roadmap", type="document")]

    def get_resource(self, resource_id):
        mime_type = (
            "application/vnd.google-apps.spreadsheet"
            if resource_id.startswith("spreadsheet")
            else "application/vnd.google-apps.document"
        )
        return WorkspaceResource(
            id=resource_id,
            name="Controlled resource",
            mime_type=mime_type,
            modified_time="2026-08-22T00:00:00Z",
            drive_id=None,
            ancestor_ids=("allowed_folder_123",),
        )


class FakeSourcePolicy:
    def authorize(self, resource):
        assert "allowed_folder_123" in resource.ancestor_ids


class RejectSourcePolicy:
    def authorize(self, _resource):
        raise WorkspaceAdapterError(
            "source_not_allowed",
            "Resource is outside approved knowledge sources.",
            403,
        )


class FakeDocsAdapter:
    max_chars = 20

    def get_document(self, resource, *, max_chars):
        assert resource.id == "document_12345"
        assert max_chars == 20
        return GoogleDocContent(
            id=resource.id,
            name="Controlled document",
            mime_type="application/vnd.google-apps.document",
            modified_time="2026-08-22T00:00:00Z",
            text="controlled content",
            truncated=False,
            limit=max_chars,
        )


class FakeSheetsAdapter:
    max_cells = 100

    def get_range(self, resource, *, range_name):
        assert resource.id == "spreadsheet_12345"
        assert range_name == "A1:F10"
        return SheetRangeContent(
            spreadsheet_id=resource.id,
            range=range_name,
            values=[["Header"], ["Value"]],
        )


def test_workspace_status_response():
    app.dependency_overrides[get_workspace_adapter] = FakeWorkspaceAdapter
    try:
        response = TestClient(app).get(
            "/workspace/status", headers={"X-Correlation-ID": "test-request-123"}
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "service": "brunova-knowledge-gateway",
        "workspace": {"connected": True, "delegated_user": "reader@example.com"},
        "request_id": "test-request-123",
    }
    assert response.headers["X-Correlation-ID"] == "test-request-123"


def test_drive_list_response():
    app.dependency_overrides[get_workspace_adapter] = FakeWorkspaceAdapter
    app.dependency_overrides[get_source_policy] = FakeSourcePolicy
    try:
        response = TestClient(app).get("/workspace/drive/list?limit=2")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "files": [
            {"id": "document_12345", "name": "Roadmap", "type": "document"}
        ],
        "request_id": response.headers["X-Correlation-ID"],
    }


def test_drive_list_rejects_unbounded_request():
    app.dependency_overrides[get_workspace_adapter] = FakeWorkspaceAdapter
    app.dependency_overrides[get_source_policy] = FakeSourcePolicy
    try:
        response = TestClient(app).get("/workspace/drive/list?limit=101")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422
    assert response.json()["detail"] == "limit must be at most 100"
    assert response.json()["request_id"] == response.headers["X-Correlation-ID"]


def test_document_content_response():
    app.dependency_overrides[get_docs_adapter] = FakeDocsAdapter
    app.dependency_overrides[get_workspace_adapter] = FakeWorkspaceAdapter
    app.dependency_overrides[get_source_policy] = FakeSourcePolicy
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
    app.dependency_overrides[get_workspace_adapter] = FakeWorkspaceAdapter
    app.dependency_overrides[get_source_policy] = FakeSourcePolicy
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
        "request_id": response.headers["X-Correlation-ID"],
    }


def test_sheet_range_is_required():
    app.dependency_overrides[get_sheets_adapter] = FakeSheetsAdapter
    app.dependency_overrides[get_workspace_adapter] = FakeWorkspaceAdapter
    app.dependency_overrides[get_source_policy] = FakeSourcePolicy
    try:
        response = TestClient(app).get("/workspace/sheets/spreadsheet_12345")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422


def test_document_outside_allowlist_is_rejected_before_content_read():
    app.dependency_overrides[get_docs_adapter] = FakeDocsAdapter
    app.dependency_overrides[get_workspace_adapter] = FakeWorkspaceAdapter
    app.dependency_overrides[get_source_policy] = RejectSourcePolicy
    try:
        response = TestClient(app).get(
            "/workspace/docs/document_12345",
            headers={"X-Correlation-ID": "denied-request-123"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 403
    assert response.json() == {
        "error": {
            "code": "source_not_allowed",
            "message": "Resource is outside approved knowledge sources.",
        },
        "request_id": "denied-request-123",
    }
