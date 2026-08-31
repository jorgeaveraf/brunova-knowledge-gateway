"""Versioned semantic registry for approved knowledge sources."""

from enum import Enum
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError


class Classification(str, Enum):
    MANAGEMENT_ONLY = "management_only"
    INTERNAL_DELIVERY = "internal_delivery"
    CLIENT_SHAREABLE = "client_shareable"
    PUBLIC = "public"


class SourceStatus(str, Enum):
    ACTIVE = "active"
    DISABLED = "disabled"


class SourceType(str, Enum):
    KNOWLEDGE_SOURCE = "knowledge_source"
    ARCHIVE_DESTINATION = "archive_destination"
    VALIDATION_SOURCE = "validation_source"


class SourceCapabilities(BaseModel):
    """Explicit operations approved for one versioned source."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    read: bool = True
    create: bool = False
    update: bool = False
    move: bool = False
    delete: bool = False
    share: bool = False
    convert: bool = False


class SourceDefinition(BaseModel):
    model_config = ConfigDict(populate_by_name=True, frozen=True)

    id: str = Field(pattern=r"^[a-z][a-z0-9_]{2,63}$")
    name: str = Field(min_length=1, max_length=100)
    system: Literal["google_workspace"]
    location_type: Literal["shared_drive", "folder"]
    location_id: str = Field(pattern=r"^[A-Za-z0-9_-]{10,200}$")
    classification: Classification
    owners: tuple[str, ...] = Field(alias="owner", min_length=1)
    status: SourceStatus
    source_type: SourceType = SourceType.KNOWLEDGE_SOURCE
    capabilities: SourceCapabilities = Field(default_factory=SourceCapabilities)


class SourceRegistryDocument(BaseModel):
    version: Literal[1]
    sources: tuple[SourceDefinition, ...]


class SourceRegistryMetadata(BaseModel):
    """Non-sensitive registry metadata safe for public gateway responses."""

    id: str
    name: str
    system: Literal["google_workspace"]
    classification: Classification
    status: SourceStatus
    source_type: SourceType
    capabilities: SourceCapabilities

    @classmethod
    def from_definition(cls, source: SourceDefinition) -> "SourceRegistryMetadata":
        return cls(
            id=source.id,
            name=source.name,
            system=source.system,
            classification=source.classification,
            status=source.status,
            source_type=source.source_type,
            capabilities=source.capabilities,
        )


class SourceRegistry:
    def __init__(self, document: SourceRegistryDocument) -> None:
        self.version = document.version
        self._sources = document.sources
        self._validate_uniqueness()

    @classmethod
    def load(cls, path: str) -> "SourceRegistry":
        try:
            with Path(path).open("r", encoding="utf-8") as registry_file:
                raw_document = yaml.safe_load(registry_file)
            return cls(SourceRegistryDocument.model_validate(raw_document))
        except (OSError, yaml.YAMLError, ValidationError, TypeError) as error:
            raise ValueError(f"Invalid source registry: {error}") from error

    @property
    def sources(self) -> tuple[SourceDefinition, ...]:
        return self._sources

    def get(self, source_id: str) -> SourceDefinition:
        for source in self._sources:
            if source.id == source_id:
                return source
        raise KeyError(source_id)

    def sources_for_location(self, location_id: str) -> tuple[SourceDefinition, ...]:
        return tuple(
            source for source in self._sources if source.location_id == location_id
        )

    def _validate_uniqueness(self) -> None:
        ids = [source.id for source in self._sources]
        locations = [source.location_id for source in self._sources]
        if len(ids) != len(set(ids)):
            raise ValueError("Source registry contains duplicate source IDs")
        if len(locations) != len(set(locations)):
            raise ValueError("Source registry contains duplicate locations")
