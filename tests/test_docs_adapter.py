from unittest.mock import Mock

from app.adapters.google_workspace.docs import GoogleDocsAdapter, _bounded_text
from app.adapters.google_workspace.models import WorkspaceResource
from app.config.settings import Settings


def settings():
    return Settings(
        workspace_delegated_user="reader@example.com",
        workspace_service_account_email="gateway@project.iam.gserviceaccount.com",
        workspace_doc_max_chars=10,
        workspace_sheet_max_cells=100,
        workspace_blocked_source_ids=(),
        workspace_source_max_depth=20,
        workspace_audit_enabled=True,
        workspace_source_registry_path="app/config/sources.yaml",
    )


def test_bounded_text_truncates_without_building_full_response():
    text, truncated = _bounded_text(iter(["hello", " world"]), 7)

    assert text == "hello w"
    assert truncated is True


def test_docs_adapter_returns_metadata_and_truncated_nested_text():
    docs_get = Mock()
    docs_get.execute.return_value = {
        "tabs": [
            {
                "documentTab": {
                    "body": {
                        "content": [
                            {
                                "paragraph": {
                                    "elements": [
                                        {"textRun": {"content": "Hello world\n"}}
                                    ]
                                }
                            }
                        ]
                    }
                }
            }
        ]
    }
    documents = Mock()
    documents.get.return_value = docs_get
    docs = Mock()
    docs.documents.return_value = documents

    builder = Mock(return_value=docs)
    adapter = GoogleDocsAdapter(
        settings(), credentials_factory=Mock(return_value=object()), service_builder=builder
    )

    resource = WorkspaceResource(
        id="document_12345",
        name="Plan",
        mime_type="application/vnd.google-apps.document",
        modified_time="2026-08-22T00:00:00Z",
        drive_id=None,
        ancestor_ids=("allowed_folder_123",),
    )
    result = adapter.get_document(resource, max_chars=5)

    assert result.model_dump() == {
        "id": "document_12345",
        "name": "Plan",
        "mime_type": "application/vnd.google-apps.document",
        "modified_time": "2026-08-22T00:00:00Z",
        "text": "Hello",
        "truncated": True,
        "limit": 5,
        "request_id": None,
        "source": None,
    }
    documents.get.assert_called_once_with(
        documentId="document_12345", includeTabsContent=True
    )


def test_docs_adapter_applies_only_append_text_batch_update():
    update_request = Mock()
    documents = Mock()
    documents.batchUpdate.return_value = update_request
    docs = Mock()
    docs.documents.return_value = documents
    adapter = GoogleDocsAdapter(
        settings(),
        credentials_factory=Mock(return_value=object()),
        service_builder=Mock(return_value=docs),
    )
    resource = WorkspaceResource(
        id="document_12345",
        name="Plan",
        mime_type="application/vnd.google-apps.document",
        modified_time="",
        drive_id=None,
        ancestor_ids=("allowed_folder_123",),
    )

    adapter.append_text(resource, text="Approved addition.")

    documents.batchUpdate.assert_called_once_with(
        documentId="document_12345",
        body={
            "requests": [
                {
                    "insertText": {
                        "endOfSegmentLocation": {},
                        "text": "Approved addition.",
                    }
                }
            ]
        },
    )
    update_request.execute.assert_called_once_with()
