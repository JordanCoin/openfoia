"""Base class for public records adapters."""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class RecordEntity:
    """A normalized entity returned by a records adapter.

    Compatible with the OpenFOIA Entity model for downstream ingestion.
    """

    entity_type: str  # "PERSON", "ORGANIZATION", etc.
    name: str
    source: str  # adapter source name (e.g. "opencorporates", "sec")
    source_url: str | None = None
    jurisdiction: str | None = None
    status: str | None = None
    identifiers: dict[str, str] = field(default_factory=dict)
    extra_data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        """Convert to plain dict for serialization."""
        return {
            "entity_type": self.entity_type,
            "name": self.name,
            "source": self.source,
            "source_url": self.source_url,
            "jurisdiction": self.jurisdiction,
            "status": self.status,
            "identifiers": self.identifiers,
            "extra_data": self.extra_data,
        }


@dataclass
class SearchResult:
    """Container for search results from a records adapter."""

    source: str
    query: str
    total_results: int
    entities: list[RecordEntity]
    page: int = 1
    per_page: int = 25
    raw_response: dict[str, Any] | None = None
    error: str | None = None  # non-None means API failed (vs genuine zero results)


class RecordAdapter(ABC):
    """Base class for public records adapters.

    Each adapter wraps an external API and returns normalized
    RecordEntity objects that can feed into the OpenFOIA entity model.
    """

    source_name: str = ""

    @abstractmethod
    async def search(self, query: str, **kwargs: Any) -> SearchResult:
        """Search for records matching the query.

        Args:
            query: Search term (company name, person name, etc.)
            **kwargs: Adapter-specific options (jurisdiction, page, etc.)

        Returns:
            SearchResult containing normalized entities.
        """
        ...

    @abstractmethod
    async def fetch(self, identifier: str, **kwargs: Any) -> RecordEntity | None:
        """Fetch a specific record by its identifier.

        Args:
            identifier: Unique identifier for the record (company number, CIK, etc.)
            **kwargs: Adapter-specific options.

        Returns:
            RecordEntity if found, None otherwise.
        """
        ...
