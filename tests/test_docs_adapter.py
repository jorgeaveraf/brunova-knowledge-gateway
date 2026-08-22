from unittest.mock import Mock

from app.adapters.google_workspace.docs import GoogleDocsAdapter, _bounded_text
from app.config.settings import Settings


def settings():
    return Settings(
        workspace_delegated_user="reader@example.com",
        workspace_service_account_email="gateway@project.iam.gserviceaccount.com",
        workspace_doc_max_chars=10,
        workspace_sheet_max_cells=100,
    )


def test_bounded_text_truncates_without_building_full_response():
    text, truncated = _bounded_text(iter(["hello", " world"]), 7)

    assert text == "hello w"
    assert truncated is True


def test_docs_adapter_returns_metadata_and_truncated_nested_text():
    drive_get = Mock()
    drive_get.execute.return_value = {
        "id": "document_12345",
        "name": "Plan",
        "mimeType": "application/vnd.google-apps.document",
        "modifiedTime": "2026-08-22T00:00:00Z",
    }
    drive_files = Mock()
    drive_files.get.return_value = drive_get
    drive = Mock()
    drive.files.return_value = drive_files

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

    builder = Mock(side_effect=[drive, docs])
    adapter = GoogleDocsAdapter(
        settings(), credentials_factory=Mock(return_value=object()), service_builder=builder
    )

    result = adapter.get_document("document_12345", max_chars=5)

    assert result.model_dump() == {
        "id": "document_12345",
        "name": "Plan",
        "mime_type": "application/vnd.google-apps.document",
        "modified_time": "2026-08-22T00:00:00Z",
        "text": "Hello",
        "truncated": True,
        "limit": 5,
    }
    drive_files.get.assert_called_once_with(
        fileId="document_12345", fields="id,name,mimeType,modifiedTime"
    )
    documents.get.assert_called_once_with(
        documentId="document_12345", includeTabsContent=True
    )
