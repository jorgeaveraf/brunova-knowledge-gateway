"""Runtime construction for non-HTTP Knowledge Gateway interfaces."""

from dataclasses import dataclass
from functools import lru_cache

from app.adapters.google_workspace.docs import GoogleDocsAdapter
from app.adapters.google_workspace.drive import GoogleWorkspaceAdapter
from app.adapters.google_workspace.errors import WorkspaceAdapterError
from app.adapters.google_workspace.sheets import GoogleSheetsAdapter
from app.config.settings import Settings, get_settings
from app.policies.source_access import SourceAccessPolicy
from app.source_registry import SourceRegistry


@dataclass(frozen=True)
class KnowledgeRuntime:
    settings: Settings
    registry: SourceRegistry
    source_policy: SourceAccessPolicy
    workspace_adapter: GoogleWorkspaceAdapter
    docs_adapter: GoogleDocsAdapter
    sheets_adapter: GoogleSheetsAdapter


@lru_cache
def get_runtime_gateway() -> KnowledgeRuntime:
    try:
        settings = get_settings()
        registry = SourceRegistry.load(settings.workspace_source_registry_path)
        return KnowledgeRuntime(
            settings=settings,
            registry=registry,
            source_policy=SourceAccessPolicy(settings, registry),
            workspace_adapter=GoogleWorkspaceAdapter(settings),
            docs_adapter=GoogleDocsAdapter(settings),
            sheets_adapter=GoogleSheetsAdapter(settings),
        )
    except ValueError as error:
        raise WorkspaceAdapterError(
            "configuration_invalid",
            "Knowledge Gateway runtime configuration is invalid.",
            503,
        ) from error
