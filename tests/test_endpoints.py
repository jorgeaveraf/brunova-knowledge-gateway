import os

from fastapi.testclient import TestClient

from app.adapters.google_workspace.models import (
    DriveFile,
    GoogleDocContent,
    SheetRangeContent,
    SourceMetadata,
    WorkspaceResource,
)
from app.adapters.google_workspace.errors import WorkspaceAdapterError
from app.policies.classification import SourceContext
from app.policies.source_access import AllowedSource
from app.source_registry import (
    Classification,
    SourceDefinition,
    SourceRegistry,
    SourceRegistryDocument,
)
from app.source_discovery.interface import (
    CandidateSource,
    DiscoveryResult,
    SourceProposalSuggestion,
    candidate_identifier,
)
from app.main import (
    app,
    get_docs_adapter,
    get_sheets_adapter,
    get_source_policy,
    get_source_discovery,
    get_source_registry,
    get_workspace_adapter,
)


TEST_SOURCE = SourceDefinition.model_validate(
    {
        "id": "career_ops",
        "name": "Career Ops",
        "system": "google_workspace",
        "location_type": "folder",
        "location_id": "allowed_folder_123",
        "classification": "management_only",
        "owner": ["Jorge", "Nat"],
        "status": "active",
    }
)
TEST_REGISTRY = SourceRegistry(
    SourceRegistryDocument(version=1, sources=(TEST_SOURCE,))
)


def authenticated_client() -> TestClient:
    return TestClient(
        app,
        headers={
            "Authorization": f"Bearer {os.environ['BRUNOVA_GATEWAY_TOKEN']}"
        },
    )


def fake_source_registry():
    return TEST_REGISTRY


class FakeDiscovery:
    def discover(self, *, limit=25):
        assert limit == 5
        candidate = CandidateSource(
            system="google_workspace",
            location_type="shared_drive",
            location_id="finance_drive_123",
            name="Finance",
            classification_suggestion=Classification.MANAGEMENT_ONLY,
            reasons=("new shared drive detected",),
        )
        return DiscoveryResult(
            candidates=(candidate,),
            proposals=(
                SourceProposalSuggestion(
                    candidate=candidate,
                    proposed_id="finance",
                    suggested_classification=Classification.MANAGEMENT_ONLY,
                    confidence="medium",
                    reasons=("new shared drive detected",),
                ),
            ),
        )


class FakeWorkspaceAdapter:
    delegated_user = "reader@example.com"

    def check_connection(self):
        return None

    def list_files(self, *, limit, source_policy):
        assert limit == 2
        return self._files()

    def list_source_files(self, *, source, limit, source_policy):
        assert source.definition.id == "career_ops"
        assert limit == 2
        return self._files()

    @staticmethod
    def _files():
        return [
            DriveFile(
                id="document_12345",
                name="Roadmap",
                type="document",
                source=SourceMetadata(
                    id="career_ops",
                    name="Career Ops",
                    classification="management_only",
                ),
            )
        ]

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
        return SourceContext(
            source_id="career_ops",
            source_name="Career Ops",
            classification=Classification.MANAGEMENT_ONLY,
        )

    def authorize_source(self, source):
        assert source == TEST_SOURCE
        return AllowedSource(definition=source, context=self.authorize_context())

    def authorize_resource_for_source(self, resource, source):
        assert "allowed_folder_123" in resource.ancestor_ids
        assert source.definition == TEST_SOURCE
        return source.context

    @staticmethod
    def authorize_context():
        return SourceContext(
            source_id="career_ops",
            source_name="Career Ops",
            classification=Classification.MANAGEMENT_ONLY,
        )


class RejectSourcePolicy:
    def authorize(self, _resource):
        raise WorkspaceAdapterError(
            "source_not_allowed",
            "Resource is outside approved knowledge sources.",
            403,
        )

    def authorize_source(self, _source):
        raise WorkspaceAdapterError(
            "source_not_allowed",
            "Resource is outside approved knowledge sources.",
            403,
        )


class RejectMembershipPolicy(FakeSourcePolicy):
    def authorize_resource_for_source(self, _resource, _source):
        raise WorkspaceAdapterError(
            "resource_not_in_source",
            "The requested resource does not belong to the selected source.",
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
        response = authenticated_client().get(
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


def test_health_remains_available_without_authentication():
    response = TestClient(app).get("/health")
    assert response.status_code == 200


def test_drive_list_response():
    app.dependency_overrides[get_workspace_adapter] = FakeWorkspaceAdapter
    app.dependency_overrides[get_source_policy] = FakeSourcePolicy
    try:
        response = authenticated_client().get("/workspace/drive/list?limit=2")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == {
        "files": [
            {
                "id": "document_12345",
                "name": "Roadmap",
                "type": "document",
                "source": {
                    "id": "career_ops",
                    "name": "Career Ops",
                    "classification": "management_only",
                },
            }
        ],
        "request_id": response.headers["X-Correlation-ID"],
    }


def test_drive_list_rejects_unbounded_request():
    app.dependency_overrides[get_workspace_adapter] = FakeWorkspaceAdapter
    app.dependency_overrides[get_source_policy] = FakeSourcePolicy
    try:
        response = authenticated_client().get("/workspace/drive/list?limit=101")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422
    assert response.json()["detail"] == "limit must be at most 100"
    assert response.json()["request_id"] == response.headers["X-Correlation-ID"]


def test_sources_list_returns_only_safe_registry_metadata():
    app.dependency_overrides[get_source_registry] = fake_source_registry
    try:
        response = authenticated_client().get("/sources")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json() == [
        {
            "id": "career_ops",
            "name": "Career Ops",
            "system": "google_workspace",
            "classification": "management_only",
            "status": "active",
            "capabilities": {
                "read": True,
                "create": False,
                "update": False,
                "move": False,
                "delete": False,
                "share": False,
            },
        }
    ]
    assert "owner" not in response.text
    assert "location_id" not in response.text
    assert "allowed_folder_123" not in response.text


def test_source_detail_returns_safe_metadata_and_missing_source_is_404():
    app.dependency_overrides[get_source_registry] = fake_source_registry
    try:
        client = authenticated_client()
        response = client.get("/sources/career_ops")
        missing = client.get("/sources/missing_source")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["id"] == "career_ops"
    assert response.json()["classification"] == "management_only"
    assert response.json()["capabilities"] == {
        "read": True,
        "create": False,
        "update": False,
        "move": False,
        "delete": False,
        "share": False,
    }
    assert "location_id" not in response.text
    assert "owner" not in response.text
    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "source_not_found"


def test_source_discovery_returns_safe_candidates_only():
    app.dependency_overrides[get_source_discovery] = FakeDiscovery
    try:
        response = authenticated_client().get("/sources/discover?limit=5")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    candidate_id = candidate_identifier(
        CandidateSource(
            system="google_workspace",
            location_type="shared_drive",
            location_id="finance_drive_123",
            name="Finance",
            classification_suggestion=Classification.MANAGEMENT_ONLY,
            reasons=("new shared drive detected",),
        )
    )
    assert response.json() == {
        "candidates": [
            {
                "candidate_id": candidate_id,
                "name": "Finance",
                "location_type": "shared_drive",
                "classification_suggestion": "management_only",
                "reason": ["new shared drive detected"],
                "exists": True,
            }
        ],
        "proposals": [
            {
                "type": "new_source_proposal",
                "candidate": {
                    "candidate_id": candidate_id,
                    "name": "Finance",
                    "location_type": "shared_drive",
                },
                "suggested_classification": "management_only",
                "confidence": "medium",
                "reason": ["new shared drive detected"],
            }
        ],
    }
    assert "finance_drive_123" not in response.text
    assert "proposed_id" not in response.text
    assert "permissions" not in response.text
    assert "content" not in response.text


def test_source_files_resolves_authorizes_and_lists_one_source():
    app.dependency_overrides[get_source_registry] = fake_source_registry
    app.dependency_overrides[get_workspace_adapter] = FakeWorkspaceAdapter
    app.dependency_overrides[get_source_policy] = FakeSourcePolicy
    try:
        response = authenticated_client().get(
            "/sources/career_ops/files?limit=2",
            headers={"X-Correlation-ID": "source-files-request"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["request_id"] == "source-files-request"
    assert response.json()["files"][0]["source"] == {
        "id": "career_ops",
        "name": "Career Ops",
        "classification": "management_only",
    }


def test_source_files_rejects_missing_and_blocked_sources():
    app.dependency_overrides[get_source_registry] = fake_source_registry
    app.dependency_overrides[get_workspace_adapter] = FakeWorkspaceAdapter
    app.dependency_overrides[get_source_policy] = RejectSourcePolicy
    try:
        client = authenticated_client()
        missing = client.get("/sources/missing_source/files?limit=2")
        blocked = client.get("/sources/career_ops/files?limit=2")
    finally:
        app.dependency_overrides.clear()

    assert missing.status_code == 404
    assert missing.json()["error"]["code"] == "source_not_found"
    assert blocked.status_code == 403
    assert blocked.json()["error"]["code"] == "source_not_allowed"


def test_source_scoped_document_response():
    app.dependency_overrides[get_source_registry] = fake_source_registry
    app.dependency_overrides[get_docs_adapter] = FakeDocsAdapter
    app.dependency_overrides[get_workspace_adapter] = FakeWorkspaceAdapter
    app.dependency_overrides[get_source_policy] = FakeSourcePolicy
    try:
        response = authenticated_client().get(
            "/sources/career_ops/docs/document_12345",
            headers={"X-Correlation-ID": "source-doc-request"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["text"] == "controlled content"
    assert response.json()["request_id"] == "source-doc-request"
    assert response.json()["source"]["id"] == "career_ops"


def test_source_scoped_sheet_response():
    app.dependency_overrides[get_source_registry] = fake_source_registry
    app.dependency_overrides[get_sheets_adapter] = FakeSheetsAdapter
    app.dependency_overrides[get_workspace_adapter] = FakeWorkspaceAdapter
    app.dependency_overrides[get_source_policy] = FakeSourcePolicy
    try:
        response = authenticated_client().get(
            "/sources/career_ops/sheets/spreadsheet_12345?range=A1:F10"
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["values"] == [["Header"], ["Value"]]
    assert response.json()["source"]["classification"] == "management_only"


def test_source_scoped_content_rejects_resource_outside_selected_source():
    app.dependency_overrides[get_source_registry] = fake_source_registry
    app.dependency_overrides[get_docs_adapter] = FakeDocsAdapter
    app.dependency_overrides[get_sheets_adapter] = FakeSheetsAdapter
    app.dependency_overrides[get_workspace_adapter] = FakeWorkspaceAdapter
    app.dependency_overrides[get_source_policy] = RejectMembershipPolicy
    try:
        client = authenticated_client()
        document = client.get("/sources/career_ops/docs/document_12345")
        sheet = client.get(
            "/sources/career_ops/sheets/spreadsheet_12345?range=A1:F10"
        )
    finally:
        app.dependency_overrides.clear()

    assert document.status_code == 403
    assert document.json()["error"]["code"] == "resource_not_in_source"
    assert sheet.status_code == 403
    assert sheet.json()["error"]["code"] == "resource_not_in_source"


def test_document_content_response():
    app.dependency_overrides[get_docs_adapter] = FakeDocsAdapter
    app.dependency_overrides[get_workspace_adapter] = FakeWorkspaceAdapter
    app.dependency_overrides[get_source_policy] = FakeSourcePolicy
    try:
        response = authenticated_client().get("/workspace/docs/document_12345")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 200
    assert response.json()["text"] == "controlled content"
    assert response.json()["limit"] == 20
    assert response.json()["truncated"] is False
    assert response.json()["source"] == {
        "id": "career_ops",
        "name": "Career Ops",
        "classification": "management_only",
    }


def test_sheet_range_response():
    app.dependency_overrides[get_sheets_adapter] = FakeSheetsAdapter
    app.dependency_overrides[get_workspace_adapter] = FakeWorkspaceAdapter
    app.dependency_overrides[get_source_policy] = FakeSourcePolicy
    try:
        response = authenticated_client().get(
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
        "source": {
            "id": "career_ops",
            "name": "Career Ops",
            "classification": "management_only",
        },
    }


def test_sheet_range_is_required():
    app.dependency_overrides[get_sheets_adapter] = FakeSheetsAdapter
    app.dependency_overrides[get_workspace_adapter] = FakeWorkspaceAdapter
    app.dependency_overrides[get_source_policy] = FakeSourcePolicy
    try:
        response = authenticated_client().get("/workspace/sheets/spreadsheet_12345")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 422


def test_document_outside_allowlist_is_rejected_before_content_read():
    app.dependency_overrides[get_docs_adapter] = FakeDocsAdapter
    app.dependency_overrides[get_workspace_adapter] = FakeWorkspaceAdapter
    app.dependency_overrides[get_source_policy] = RejectSourcePolicy
    try:
        response = authenticated_client().get(
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
