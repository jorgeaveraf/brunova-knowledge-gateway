"""Runtime construction for non-HTTP Knowledge Gateway interfaces."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from app.adapters.google_workspace.docs import GoogleDocsAdapter
from app.adapters.google_workspace.drive import GoogleWorkspaceAdapter
from app.adapters.google_workspace.errors import WorkspaceAdapterError
from app.adapters.google_workspace.sheets import GoogleSheetsAdapter
from app.adapters.google_cloud_logging import CloudLoggingOperationHistoryStore
from app.artifact_refs import ArtifactReferenceCodec
from app.config.settings import Settings, get_settings
from app.policies.source_access import SourceAccessPolicy
from app.policies.content_mutation import ContentMutationPolicy
from app.source_discovery.google_workspace import GoogleWorkspaceSourceDiscovery
from app.source_discovery.interface import SourceDiscovery
from app.source_proposal_store import (
    CloudStorageProposalObjectBackend,
    SourceProposalStore,
    YamlSourceProposalStore,
)
from app.source_registry import SourceRegistry
from app.operation_history import OperationHistoryStore
from app.agent_signals import AgentSignalInbox, CloudStorageAgentSignalBackend


@dataclass(frozen=True)
class KnowledgeRuntime:
    settings: Settings
    registry: SourceRegistry
    source_policy: SourceAccessPolicy
    workspace_adapter: GoogleWorkspaceAdapter
    docs_adapter: GoogleDocsAdapter
    sheets_adapter: GoogleSheetsAdapter
    source_discovery: SourceDiscovery
    proposal_store: SourceProposalStore
    mutation_policy: ContentMutationPolicy
    operation_history_store: OperationHistoryStore | None = None
    artifact_reference_codec: ArtifactReferenceCodec | None = None
    agent_signal_inbox: AgentSignalInbox | None = None


def _build_agent_signal_inbox(settings: Settings) -> AgentSignalInbox | None:
    """Keep optional Inbox configuration isolated from every other provider."""

    if not settings.agent_signal_bucket:
        return None
    try:
        return AgentSignalInbox(
            CloudStorageAgentSignalBackend(
                bucket_name=settings.agent_signal_bucket,
                prefix=settings.agent_signal_prefix,
            ),
            claim_lease_seconds=settings.agent_signal_claim_lease_seconds,
            completed_retention_days=settings.agent_signal_completed_retention_days,
        )
    except Exception:
        return None


@lru_cache
def get_runtime_gateway() -> KnowledgeRuntime:
    try:
        settings = get_settings()
        registry = SourceRegistry.load(settings.workspace_source_registry_path)
        workspace_adapter = GoogleWorkspaceAdapter(settings)
        source_policy = SourceAccessPolicy(settings, registry)
        return KnowledgeRuntime(
            settings=settings,
            registry=registry,
            source_policy=source_policy,
            workspace_adapter=workspace_adapter,
            docs_adapter=GoogleDocsAdapter(settings),
            sheets_adapter=GoogleSheetsAdapter(settings),
            source_discovery=GoogleWorkspaceSourceDiscovery(
                workspace_adapter,
                registry,
                blocked_location_ids=settings.workspace_blocked_source_ids,
            ),
            proposal_store=YamlSourceProposalStore(
                CloudStorageProposalObjectBackend(
                    bucket_name=settings.source_proposal_bucket,
                    object_name=settings.source_proposal_object,
                )
            ),
            mutation_policy=ContentMutationPolicy(registry, source_policy),
            artifact_reference_codec=ArtifactReferenceCodec.from_environment(),
            operation_history_store=CloudLoggingOperationHistoryStore(),
            agent_signal_inbox=_build_agent_signal_inbox(settings),
        )
    except ValueError as error:
        raise WorkspaceAdapterError(
            "configuration_invalid",
            "Knowledge Gateway runtime configuration is invalid.",
            503,
        ) from error
