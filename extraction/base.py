"""Extractor contract. Implementations are selected from `extraction.provider` in config.yaml."""

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Protocol

from db.models import JsonObject


@dataclass(frozen=True)
class ExtractedDocument:
    """Clean text pulled from one source, plus the identity used to track it."""

    source_type: str
    source_uri: str  # stable identifier; with source_type it is the sources unique key
    text: str
    metadata: JsonObject = field(default_factory=dict)


class Extractor(Protocol):
    def extract(self) -> Iterable[ExtractedDocument]:
        """Yield every document currently present in the source."""
        ...
