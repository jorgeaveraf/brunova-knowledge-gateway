import asyncio
import importlib
from unittest.mock import Mock

from mcp import Client

from app.adapters.google_workspace.models import (
    DriveFile,
    GoogleDocContent,
    SheetRangeContent,
    SourceMetadata,
    WorkspaceResource,
)
from app.config.settings import Settings
from app.policies.source_access import SourceAccessPolicy
from app.runtime import KnowledgeRuntime
from app.source_registry import SourceDefinition, SourceRegistry, SourceRegistryDocument
from app.source_discovery.interface import (
    CandidateSource,
    DiscoveryResult,
    SourceProposalSuggestion,
    candidate_identifier,
)
from app.source_proposal_store import ProposalObjectConflict, YamlSourceProposalStore

mcp_module = importlib.import_module("app.mcp_server")


def settings():
    return Settings(
        workspace_delegated_user="reader@example.com",
        workspace_service_account_email="gateway@project.iam.gserviceaccount.com",
        workspace_doc_max_chars=100,
        workspace_sheet_max_cells=100,
        workspace_blocked_source_ids=(),
        workspace_source_max_depth=20,
        workspace_audit_enabled=True,
        workspace_source_registry_path="unused.yaml",
    )


def registry():
    source = SourceDefinition.model_validate(
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
    return SourceRegistry(SourceRegistryDocument(version=1, sources=(source,)))


class FakeWorkspaceAdapter:
    def __init__(self, *, in_source=True):
        self.in_source = in_source

    def list_source_files(self, *, source, limit, source_policy):
        assert source.definition.id == "career_ops"
        assert limit == 100
        return [
            DriveFile(
                id="document_12345",
                name="Career Roadmap",
                type="document",
                source=SourceMetadata(
                    id="career_ops",
                    name="Career Ops",
                    classification="management_only",
                ),
            ),
            DriveFile(
                id="sheet_123456789",
                name="Metrics",
                type="spreadsheet",
                source=SourceMetadata(
                    id="career_ops",
                    name="Career Ops",
                    classification="management_only",
                ),
            ),
        ]

    def get_resource(self, resource_id):
        return WorkspaceResource(
            id=resource_id,
            name="Controlled resource",
            mime_type=(
                "application/vnd.google-apps.spreadsheet"
                if resource_id.startswith("spreadsheet")
                else "application/vnd.google-apps.document"
            ),
            modified_time="2026-08-22T00:00:00Z",
            drive_id=None,
            ancestor_ids=(
                ("allowed_folder_123",)
                if self.in_source
                else ("another_folder_123",)
            ),
        )


class FakeDocsAdapter:
    max_chars = 100

    def get_document(self, resource, *, max_chars):
        return GoogleDocContent(
            id=resource.id,
            name=resource.name,
            mime_type=resource.mime_type,
            modified_time=resource.modified_time,
            text="authorized content",
            truncated=False,
            limit=max_chars,
        )


class FakeSheetsAdapter:
    max_cells = 100

    def get_range(self, resource, *, range_name):
        return SheetRangeContent(
            spreadsheet_id=resource.id,
            range=range_name,
            values=[["Header"], ["Value"]],
        )


class FakeDiscovery:
    def __init__(self, source_registry):
        self.source_registry = source_registry

    def discover(self, *, limit=25):
        assert limit in (10, 100)
        before = self.source_registry.sources
        candidate = CandidateSource(
            system="google_workspace",
            location_type="shared_drive",
            location_id="finance_drive_123",
            name="Finance",
            classification_suggestion="management_only",
            reasons=("new shared drive detected",),
        )
        result = DiscoveryResult(
            candidates=(candidate,),
            proposals=(
                SourceProposalSuggestion(
                    candidate=candidate,
                    proposed_id="finance",
                    suggested_classification="management_only",
                    confidence="medium",
                    reasons=candidate.reasons,
                ),
            ),
        )
        assert self.source_registry.sources == before
        return result


class MemoryObjectBackend:
    def __init__(self):
        self.content = None
        self.generation = 0

    def read(self):
        return self.content, self.generation

    def write(self, content, *, generation):
        if generation != self.generation:
            raise ProposalObjectConflict
        self.content = content
        self.generation += 1


def runtime(*, in_source=True):
    source_registry = registry()
    runtime_settings = settings()
    return KnowledgeRuntime(
        settings=runtime_settings,
        registry=source_registry,
        source_policy=SourceAccessPolicy(runtime_settings, source_registry),
        workspace_adapter=FakeWorkspaceAdapter(in_source=in_source),
        docs_adapter=FakeDocsAdapter(),
        sheets_adapter=FakeSheetsAdapter(),
        source_discovery=FakeDiscovery(source_registry),
        proposal_store=YamlSourceProposalStore(MemoryObjectBackend()),
    )


def run(coro):
    return asyncio.run(coro)


def test_mcp_exposes_only_the_nine_governed_tools(monkeypatch):
    monkeypatch.setattr(mcp_module, "get_runtime_gateway", lambda: runtime())

    async def scenario():
        async with Client(mcp_module.mcp_server) as client:
            return await client.list_tools()

    result = run(scenario())

    assert {tool.name for tool in result.tools} == {
        "list_sources",
        "list_source_documents",
        "retrieve_document",
        "retrieve_sheet_range",
        "discover_source_candidates",
        "get_source_candidate_details",
        "create_source_proposal",
        "list_source_proposals",
        "get_source_proposal",
    }


def test_mcp_discovers_safe_source_proposals_and_audits_count(monkeypatch):
    monkeypatch.setattr(mcp_module, "get_runtime_gateway", lambda: runtime())
    audit = Mock()
    monkeypatch.setattr(mcp_module, "emit_audit_record", audit)

    async def scenario():
        async with Client(mcp_module.mcp_server) as client:
            return await client.call_tool(
                "discover_source_candidates",
                {"limit": 10},
            )

    result = run(scenario())

    assert result.is_error is False
    candidate_id = candidate_identifier(
        CandidateSource(
            system="google_workspace",
            location_type="shared_drive",
            location_id="finance_drive_123",
            name="Finance",
            classification_suggestion="management_only",
            reasons=("new shared drive detected",),
        )
    )
    assert result.structured_content["candidates"][0] == {
        "candidate_id": candidate_id,
        "name": "Finance",
        "location_type": "shared_drive",
        "classification_suggestion": "management_only",
        "reason": ["new shared drive detected"],
        "exists": True,
    }
    assert result.structured_content["proposals"][0]["confidence"] == "medium"
    assert "location_id" not in repr(result.structured_content)
    assert "proposed_id" not in repr(result.structured_content)
    assert audit.call_args.kwargs["action"] == "discover_source_candidates"
    assert audit.call_args.kwargs["candidate_count"] == 1


def test_mcp_candidate_details_and_proposal_creation_are_governed(monkeypatch):
    gateway_runtime = runtime()
    before = gateway_runtime.registry.sources
    monkeypatch.setattr(mcp_module, "get_runtime_gateway", lambda: gateway_runtime)
    audit = Mock()
    monkeypatch.setattr(mcp_module, "emit_audit_record", audit)
    candidate_id = candidate_identifier(
        CandidateSource(
            system="google_workspace",
            location_type="shared_drive",
            location_id="finance_drive_123",
            name="Finance",
            classification_suggestion="management_only",
            reasons=("new shared drive detected",),
        )
    )

    async def scenario():
        async with Client(mcp_module.mcp_server) as client:
            details = await client.call_tool(
                "get_source_candidate_details",
                {"candidate_id": candidate_id},
            )
            proposal = await client.call_tool(
                "create_source_proposal",
                {
                    "candidate_id": candidate_id,
                    "name": "Finance",
                    "classification": "management_only",
                    "reason": "Reviewed; awaiting explicit human approval.",
                },
            )
            proposals = await client.call_tool("list_source_proposals", {})
            proposal_details = await client.call_tool(
                "get_source_proposal",
                {"proposal_id": proposal.structured_content["proposal_id"]},
            )
            return details, proposal, proposals, proposal_details

    details, proposal, proposals, proposal_details = run(scenario())

    assert details.is_error is False
    assert details.structured_content["candidate"]["candidate_id"] == candidate_id
    assert details.structured_content["candidate"]["confidence"] == "medium"
    assert "location_id" not in repr(details.structured_content)
    assert proposal.is_error is False
    assert proposal.structured_content["status"] == "pending_review"
    assert proposal.structured_content["proposal_id"].startswith("proposal_")
    assert proposals.is_error is False
    assert proposals.structured_content["proposals"] == [
        {
            "proposal_id": proposal.structured_content["proposal_id"],
            "name": "Finance",
            "classification": "management_only",
            "status": "pending_review",
        }
    ]
    assert proposal_details.is_error is False
    assert proposal_details.structured_content["proposal"]["candidate"]["name"] == (
        "Finance"
    )
    assert proposal_details.structured_content["proposal"]["confidence"] == "medium"
    assert "location_id" not in repr(proposal_details.structured_content)
    assert "request_id" not in proposal_details.structured_content["proposal"]
    assert gateway_runtime.registry.sources == before
    actions = [call.kwargs["action"] for call in audit.call_args_list]
    assert actions == [
        "get_source_candidate_details",
        "create_source_proposal",
        "list_source_proposals",
        "get_source_proposal",
    ]
    assert audit.call_args.kwargs["proposal_id"] == (
        proposal.structured_content["proposal_id"]
    )
    assert "reason" not in audit.call_args.kwargs


def test_mcp_candidate_details_reject_unknown_candidate(monkeypatch):
    monkeypatch.setattr(mcp_module, "get_runtime_gateway", lambda: runtime())
    audit = Mock()
    monkeypatch.setattr(mcp_module, "emit_audit_record", audit)

    async def scenario():
        async with Client(mcp_module.mcp_server) as client:
            return await client.call_tool(
                "get_source_candidate_details",
                {"candidate_id": "candidate_00000000000000000000000000000000"},
            )

    result = run(scenario())

    assert result.is_error is True
    assert "source_candidate_not_found" in result.content[0].text
    assert audit.call_args.kwargs["error_code"] == "source_candidate_not_found"


def test_mcp_lists_sources_and_filters_authorized_documents(monkeypatch):
    monkeypatch.setattr(mcp_module, "get_runtime_gateway", lambda: runtime())
    monkeypatch.setattr(mcp_module, "emit_audit_record", Mock())

    async def scenario():
        async with Client(mcp_module.mcp_server) as client:
            sources = await client.call_tool("list_sources", {})
            documents = await client.call_tool(
                "list_source_documents",
                {"source_id": "career_ops", "query": "road"},
            )
            return sources, documents

    sources, documents = run(scenario())

    assert sources.is_error is False
    assert sources.structured_content["sources"][0]["id"] == "career_ops"
    assert documents.is_error is False
    assert [item["name"] for item in documents.structured_content["documents"]] == [
        "Career Roadmap"
    ]


def test_mcp_retrieval_tools_apply_source_and_content_policies(monkeypatch):
    monkeypatch.setattr(mcp_module, "get_runtime_gateway", lambda: runtime())
    audit = Mock()
    monkeypatch.setattr(mcp_module, "emit_audit_record", audit)

    async def scenario():
        async with Client(mcp_module.mcp_server) as client:
            document = await client.call_tool(
                "retrieve_document",
                {"source_id": "career_ops", "document_id": "document_12345"},
            )
            sheet = await client.call_tool(
                "retrieve_sheet_range",
                {
                    "source_id": "career_ops",
                    "spreadsheet_id": "spreadsheet_12345",
                    "range": "A1:A2",
                },
            )
            return document, sheet

    document, sheet = run(scenario())

    assert document.is_error is False
    assert document.structured_content["source"]["id"] == "career_ops"
    assert sheet.is_error is False
    assert sheet.structured_content["values"] == [["Header"], ["Value"]]
    assert any(
        call.kwargs.get("source_classification") == "management_only"
        for call in audit.call_args_list
    )


def test_mcp_propagates_resource_not_in_source_error(monkeypatch):
    monkeypatch.setattr(
        mcp_module,
        "get_runtime_gateway",
        lambda: runtime(in_source=False),
    )
    audit = Mock()
    monkeypatch.setattr(mcp_module, "emit_audit_record", audit)

    async def scenario():
        async with Client(mcp_module.mcp_server) as client:
            return await client.call_tool(
                "retrieve_document",
                {"source_id": "career_ops", "document_id": "document_12345"},
            )

    result = run(scenario())

    assert result.is_error is True
    assert "resource_not_in_source" in result.content[0].text
    assert audit.call_args.kwargs["error_code"] == "resource_not_in_source"
