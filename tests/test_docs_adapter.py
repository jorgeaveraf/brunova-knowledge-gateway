from unittest.mock import Mock

import pytest
from googleapiclient.errors import HttpError

from app.adapters.google_workspace.docs import (
    GoogleDocsAdapter,
    _bounded_text,
    _operation_requests,
)
from app.adapters.google_workspace.errors import WorkspaceAdapterError
from app.adapters.google_workspace.models import WorkspaceResource
from app.artifact_refs import ArtifactReferenceCodec
from app.config.settings import Settings
from app.document_production import (
    CreateFooterOperation,
    CreateHeaderOperation,
    DeleteContentOperation,
    DeleteTableRowOperation,
    InsertTableColumnOperation,
    InsertTableOperation,
    InsertTableRowOperation,
    InsertTextOperation,
    ListOperation,
    ParagraphStyleOperation,
    ReplaceAllTextOperation,
    TableCellStyleOperation,
    TextStyleOperation,
    UpdateTableCellOperation,
)
from app.visual_assets import (
    InsertGoogleDocImageOperation,
    ReplaceGoogleDocImageOperation,
)


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


def test_inspect_structure_returns_revision_tabs_tables_segments_and_safe_styles():
    docs_get = Mock()
    docs_get.execute.return_value = {
        "revisionId": "revision-7",
        "title": "Controlled Doc",
        "documentStyle": {
            "pageSize": {"width": {"magnitude": 612, "unit": "PT"}},
            "marginTop": {"magnitude": 72, "unit": "PT"},
            "secretField": "must-not-leak",
        },
        "inlineObjects": {"image-1": {}},
        "tabs": [
            {
                "tabProperties": {"tabId": "tab-1", "title": "Main"},
                "documentTab": {
                    "body": {
                        "content": [
                            {
                                "startIndex": 1,
                                "endIndex": 14,
                                "paragraph": {
                                    "paragraphStyle": {"namedStyleType": "HEADING_1"},
                                    "elements": [
                                        {
                                            "textRun": {
                                                "content": "Overview {{x}}\n",
                                                "textStyle": {"bold": True},
                                            }
                                        }
                                    ],
                                },
                            },
                            {
                                "startIndex": 14,
                                "endIndex": 20,
                                "table": {
                                    "tableRows": [
                                        {
                                            "tableCells": [
                                                {
                                                    "startIndex": 15,
                                                    "endIndex": 19,
                                                    "content": [
                                                        {
                                                            "startIndex": 16,
                                                            "endIndex": 18,
                                                            "paragraph": {
                                                                "elements": [
                                                                    {"textRun": {"content": "A\n"}}
                                                                ]
                                                            },
                                                        }
                                                    ],
                                                }
                                            ]
                                        }
                                    ]
                                },
                            },
                        ]
                    },
                    "headers": {
                        "header-1": {
                            "content": [
                                {
                                    "startIndex": 0,
                                    "endIndex": 2,
                                    "paragraph": {"elements": [{"textRun": {"content": "H\n"}}]},
                                }
                            ]
                        }
                    },
                },
            }
        ],
    }
    documents = Mock()
    documents.get.return_value = docs_get
    docs = Mock()
    docs.documents.return_value = documents
    adapter = GoogleDocsAdapter(
        settings(), credentials_factory=Mock(return_value=object()), service_builder=Mock(return_value=docs)
    )
    resource = WorkspaceResource(
        id="document_12345",
        name="Controlled Doc",
        mime_type="application/vnd.google-apps.document",
        modified_time="",
        drive_id=None,
        ancestor_ids=("allowed_folder_123",),
    )

    result = adapter.inspect_structure(
        resource,
        artifact_ref="artifact_opaque",
        source_id="career_ops",
        reference_codec=ArtifactReferenceCodec.for_testing(),
    )

    assert result.revision_id == "revision-7"
    assert result.tabs[0].paragraphs[0].named_style_type == "HEADING_1"
    assert result.tabs[0].tab_ref.startswith("tab_")
    assert "tab-1" not in result.model_dump_json()
    assert result.tabs[0].tables[0].cells[0].text == "A\n"
    assert result.headers[0].segment_id == "header-1"
    assert result.placeholders == ["{{x}}"]
    assert result.image_count == 1
    assert result.images[0].kind == "inline"
    assert "image-1" not in result.images[0].image_ref
    assert "secretField" not in result.document_style


def test_structured_edit_maps_semantic_operations_and_requires_revision():
    update_request = Mock()
    update_request.execute.return_value = {
        "writeControl": {"requiredRevisionId": "revision-8"}
    }
    documents = Mock()
    documents.batchUpdate.return_value = update_request
    docs = Mock()
    docs.documents.return_value = documents
    adapter = GoogleDocsAdapter(
        settings(), credentials_factory=Mock(return_value=object()), service_builder=Mock(return_value=docs)
    )
    resource = WorkspaceResource(
        id="document_12345",
        name="Controlled Doc",
        mime_type="application/vnd.google-apps.document",
        modified_time="",
        drive_id=None,
        ancestor_ids=("allowed_folder_123",),
    )
    operations = [
        ReplaceAllTextOperation(operation="replace_all_text", find="{{name}}", replace="Brunova"),
        ParagraphStyleOperation(
            operation="apply_paragraph_style",
            start_index=1,
            end_index=8,
            named_style_type="TITLE",
            alignment="CENTER",
        ),
        TextStyleOperation(
            operation="apply_text_style", start_index=1, end_index=8, bold=True, font_size=18
        ),
        InsertTableOperation(operation="insert_table", index=9, rows=2, columns=2),
        CreateHeaderOperation(operation="create_header"),
    ]

    revision = adapter.edit_structure(
        resource,
        required_revision_id="revision-7",
        operations=operations,
        tab_id_resolver=lambda value: value,
    )

    assert revision == "revision-8"
    body = documents.batchUpdate.call_args.kwargs["body"]
    assert body["writeControl"] == {"requiredRevisionId": "revision-7"}
    assert [next(iter(request)) for request in body["requests"]] == [
        "replaceAllText",
        "updateParagraphStyle",
        "updateTextStyle",
        "insertTable",
        "createHeader",
    ]
    assert "batchUpdate" not in str(operations)


def test_all_structured_requests_are_allowlisted_and_never_accept_raw_batch_update():
    requests = _operation_requests(
        ReplaceAllTextOperation(operation="replace_all_text", find="old", replace="new")
    )
    assert requests == [
        {
            "replaceAllText": {
                "containsText": {"text": "old", "matchCase": True},
                "replaceText": "new",
            }
        }
    ]


def test_docs_image_edit_uses_governed_uris_refs_and_revision_control():
    update_request = Mock()
    update_request.execute.return_value = {
        "writeControl": {"requiredRevisionId": "revision-9"}
    }
    documents = Mock()
    documents.batchUpdate.return_value = update_request
    docs = Mock()
    docs.documents.return_value = documents
    adapter = GoogleDocsAdapter(
        settings(), credentials_factory=Mock(return_value=object()), service_builder=Mock(return_value=docs)
    )
    resource = WorkspaceResource(
        id="document_12345",
        name="Controlled Doc",
        mime_type="application/vnd.google-apps.document",
        modified_time="",
        drive_id=None,
        ancestor_ids=("allowed_folder_123",),
    )
    operations = [
        (
            InsertGoogleDocImageOperation(
                operation="insert_image", asset_ref="asset_opaque", index=8,
                tab_ref="tab_opaque", width_points=72,
            ),
            "https://signed.example/insert",
        ),
        (
            ReplaceGoogleDocImageOperation(
                operation="replace_image", asset_ref="asset_opaque",
                image_ref="doc_image_opaque",
            ),
            "https://signed.example/replace",
        ),
    ]
    revision = adapter.edit_images(
        resource,
        required_revision_id="revision-8",
        operations_with_uris=operations,
        tab_id_resolver=lambda _: "tab-internal",
        image_ref_resolver=lambda _: ("image-internal", "tab-internal"),
    )
    assert revision == "revision-9"
    body = documents.batchUpdate.call_args.kwargs["body"]
    assert body["writeControl"] == {"requiredRevisionId": "revision-8"}
    assert body["requests"] == [
        {"insertInlineImage": {
            "uri": "https://signed.example/insert",
            "location": {"index": 8, "tabId": "tab-internal"},
            "objectSize": {"width": {"magnitude": 72.0, "unit": "PT"}},
        }},
        {"replaceImage": {
            "imageObjectId": "image-internal",
            "uri": "https://signed.example/replace",
            "imageReplaceMethod": "CENTER_CROP",
            "tabId": "tab-internal",
        }},
    ]


def test_structured_request_allowlist_covers_text_lists_tables_and_segments():
    operations = [
        InsertTextOperation(operation="insert_text_at_index", index=2, text="Body"),
        DeleteContentOperation(operation="delete_content_range", start_index=2, end_index=4),
        ListOperation(operation="create_list", start_index=2, end_index=8, list_type="bullet"),
        ListOperation(operation="create_list", start_index=2, end_index=8, list_type="numbered"),
        InsertTableRowOperation(
            operation="insert_table_row", table_start_index=10, row_index=0, column_index=0
        ),
        InsertTableColumnOperation(
            operation="insert_table_column", table_start_index=10, row_index=0, column_index=0
        ),
        DeleteTableRowOperation(
            operation="delete_table_row", table_start_index=10, row_index=0, column_index=0
        ),
        UpdateTableCellOperation(
            operation="update_table_cell_content", start_index=12, end_index=14, text="Value"
        ),
        TableCellStyleOperation(
            operation="apply_table_cell_style",
            table_start_index=10,
            row_index=0,
            column_index=0,
            background_color="#112233",
            vertical_alignment="MIDDLE",
        ),
        CreateFooterOperation(operation="create_footer"),
        InsertTextOperation(
            operation="insert_text_at_index",
            index=0,
            text="Header",
            segment_id="header-1",
        ),
    ]

    keys = [
        next(iter(request))
        for operation in operations
        for request in _operation_requests(operation)
    ]

    assert keys == [
        "insertText",
        "deleteContentRange",
        "createParagraphBullets",
        "createParagraphBullets",
        "insertTableRow",
        "insertTableColumn",
        "deleteTableRow",
        "deleteContentRange",
        "insertText",
        "updateTableCellStyle",
        "createFooter",
        "insertText",
    ]
    assert _operation_requests(operations[-1])[0]["insertText"]["location"] == {
        "index": 0,
        "segmentId": "header-1",
    }

    with pytest.raises(WorkspaceAdapterError) as body_zero:
        _operation_requests(
            InsertTextOperation(operation="insert_text_at_index", index=0, text="invalid")
        )
    assert body_zero.value.code == "document_operation_invalid"


def test_tab_scoped_header_and_footer_accept_first_section_break_at_zero():
    resolver = lambda value: {"tab-en": "internal-en"}[value]

    header = _operation_requests(
        CreateHeaderOperation(
            operation="create_header", tab_ref="tab-en", section_index=0
        ),
        tab_id_resolver=resolver,
    )
    footer = _operation_requests(
        CreateFooterOperation(
            operation="create_footer", tab_ref="tab-en", section_index=0
        ),
        tab_id_resolver=resolver,
    )

    expected_location = {"index": 0, "tabId": "internal-en"}
    assert header == [
        {
            "createHeader": {
                "type": "DEFAULT",
                "sectionBreakLocation": expected_location,
            }
        }
    ]
    assert footer == [
        {
            "createFooter": {
                "type": "DEFAULT",
                "sectionBreakLocation": expected_location,
            }
        }
    ]


def test_structured_edit_fails_safely_on_stale_revision():
    response = Mock(status=400, reason="Bad Request")
    update_request = Mock()
    update_request.execute.side_effect = HttpError(
        response,
        b'{"error":{"message":"requiredRevisionId does not match current revision"}}',
    )
    documents = Mock()
    documents.batchUpdate.return_value = update_request
    docs = Mock()
    docs.documents.return_value = documents
    adapter = GoogleDocsAdapter(
        settings(), credentials_factory=Mock(return_value=object()), service_builder=Mock(return_value=docs)
    )
    resource = WorkspaceResource(
        id="document_12345",
        name="Controlled Doc",
        mime_type="application/vnd.google-apps.document",
        modified_time="",
        drive_id=None,
        ancestor_ids=("allowed_folder_123",),
    )

    with pytest.raises(WorkspaceAdapterError) as captured:
        adapter.edit_structure(
            resource,
            required_revision_id="stale-revision",
            operations=[
                ReplaceAllTextOperation(operation="replace_all_text", find="old", replace="new")
            ],
            tab_id_resolver=lambda value: value,
        )

    assert captured.value.code == "document_revision_conflict"
    assert captured.value.status_code == 409


def test_tab_mutations_map_to_allowlisted_google_requests_with_revision_control():
    update_request = Mock()
    update_request.execute.side_effect = [
        {"writeControl": {"requiredRevisionId": "revision-2"}},
        {"writeControl": {"requiredRevisionId": "revision-3"}},
        {"writeControl": {"requiredRevisionId": "revision-4"}},
    ]
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
        name="Controlled Doc",
        mime_type="application/vnd.google-apps.document",
        modified_time="",
        drive_id=None,
        ancestor_ids=("allowed_folder_123",),
    )

    assert (
        adapter.create_tab(
            resource,
            title="ES",
            index=1,
            parent_tab_id=None,
            required_revision_id="revision-1",
        )
        == "revision-2"
    )
    assert (
        adapter.rename_tab(
            resource,
            tab_id="google-tab-1",
            title="EN",
            required_revision_id="revision-2",
        )
        == "revision-3"
    )
    assert (
        adapter.delete_tab(
            resource,
            tab_id="google-tab-2",
            required_revision_id="revision-3",
        )
        == "revision-4"
    )

    bodies = [call.kwargs["body"] for call in documents.batchUpdate.call_args_list]
    assert bodies == [
        {
            "requests": [
                {
                    "addDocumentTab": {
                        "tabProperties": {"title": "ES", "index": 1}
                    }
                }
            ],
            "writeControl": {"requiredRevisionId": "revision-1"},
        },
        {
            "requests": [
                {
                    "updateDocumentTabProperties": {
                        "tabProperties": {"tabId": "google-tab-1", "title": "EN"},
                        "fields": "title",
                    }
                }
            ],
            "writeControl": {"requiredRevisionId": "revision-2"},
        },
        {
            "requests": [{"deleteTab": {"tabId": "google-tab-2"}}],
            "writeControl": {"requiredRevisionId": "revision-3"},
        },
    ]


def test_tab_scoped_semantic_request_resolves_opaque_reference_internally():
    operation = InsertTextOperation(
        operation="insert_text_at_index",
        index=1,
        text="Spanish content",
        tab_ref="tab_opaque",
    )

    requests = _operation_requests(
        operation,
        tab_id_resolver=lambda value: {
            "tab_opaque": "google-internal-tab-id"
        }[value],
    )

    assert requests == [
        {
            "insertText": {
                "location": {"index": 1, "tabId": "google-internal-tab-id"},
                "text": "Spanish content",
            }
        }
    ]
    assert "tab_opaque" not in str(requests)


def test_tab_mutation_reports_revision_conflict_without_retrying():
    response = Mock(status=409, reason="Conflict")
    update_request = Mock()
    update_request.execute.side_effect = HttpError(
        response,
        b'{"error":{"message":"requiredRevisionId does not match current revision"}}',
    )
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
        name="Controlled Doc",
        mime_type="application/vnd.google-apps.document",
        modified_time="",
        drive_id=None,
        ancestor_ids=("allowed_folder_123",),
    )

    with pytest.raises(WorkspaceAdapterError) as captured:
        adapter.create_tab(
            resource,
            title="ES",
            required_revision_id="stale-revision",
        )

    assert captured.value.code == "document_revision_conflict"
    assert documents.batchUpdate.call_count == 1
