"""JSON, JSON Lines and YAML reader.

Structured data has no prose to split, so the structure itself becomes the structure: a list of
records turns into one table row per record, and a nested mapping turns into a paragraph of its
own leaf values, with the path through the document as its section. That way a retrieved chunk
still says where in the file it came from.
"""

import json
import logging
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import yaml

from db.models import JsonObject
from extraction.base import DocumentElement, ElementKind
from extraction.elements import ElementList
from extraction.readers.base import SECTION_SEPARATOR, ReaderError, ReadResult
from extraction.tables import header_names, table_row_element
from extraction.text import decode_bytes, squash

logger = logging.getLogger(__name__)

MAX_DEPTH = 20  # deeper nesting is rendered as a value rather than walked
FIELD_SEPARATOR = " | "

Json = Any


def _scalar(value: Json) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, str):
        return squash(value)
    if isinstance(value, (int, float)):
        return str(value)
    return squash(json.dumps(value, ensure_ascii=False, default=str))


def _is_record(value: Json) -> bool:
    return isinstance(value, dict)


def _record_keys(records: list[Json]) -> list[str]:
    """The keys of a list of records, in the order they first appear."""
    keys: dict[str, None] = {}
    for record in records:
        for key in record:
            keys.setdefault(str(key), None)
    return list(keys)


class StructuredReader:
    """Reads .json, .jsonl/.ndjson and .yaml/.yml."""

    @property
    def name(self) -> str:
        return "structured"

    @property
    def extensions(self) -> tuple[str, ...]:
        return (".json", ".jsonl", ".ndjson", ".yaml", ".yml")

    def read(self, path: Path) -> ReadResult:
        text, encoding = decode_bytes(path.read_bytes())
        documents = list(self._parse(path, text))
        elements = ElementList()
        walker = _Walker(elements, path.stem.replace("_", " "))
        for number, document in enumerate(documents, start=1):
            walker.walk(document, path="", page=number if len(documents) > 1 else 0)
        document_text, placed = elements.build()
        metadata: JsonObject = {"encoding": encoding, "documents": len(documents)}
        return ReadResult(document_text, placed, metadata)

    def _parse(self, path: Path, text: str) -> Iterator[Json]:
        suffix = path.suffix.lower()
        if suffix in (".yaml", ".yml"):
            try:
                yield from (d for d in yaml.safe_load_all(text) if d is not None)
            except yaml.YAMLError as exc:
                raise ReaderError(f"could not parse {path.name} as YAML: {exc}") from exc
            return
        if suffix in (".jsonl", ".ndjson"):
            for number, line in enumerate(text.splitlines(), start=1):
                if not line.strip():
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError as exc:
                    logger.warning(
                        "%s line %d: skipping unparseable JSON: %s", path.name, number, exc
                    )
            return
        try:
            yield json.loads(text)
        except json.JSONDecodeError as exc:
            raise ReaderError(f"could not parse {path.name} as JSON: {exc}") from exc


class _Walker:
    def __init__(self, elements: ElementList, root_name: str) -> None:
        self._elements = elements
        self._root = root_name

    def walk(self, value: Json, *, path: str, page: int, depth: int = 0) -> None:
        if depth > MAX_DEPTH:
            self._paragraph(path, _scalar(value), page)
            return
        if isinstance(value, dict):
            self._mapping(value, path=path, page=page, depth=depth)
        elif isinstance(value, list):
            self._sequence(value, path=path, page=page, depth=depth)
        else:
            self._paragraph(path, _scalar(value), page)

    def _mapping(self, mapping: dict[str, Json], *, path: str, page: int, depth: int) -> None:
        leaves: list[tuple[str, str]] = []
        for raw_key, value in mapping.items():
            key = str(raw_key)
            if isinstance(value, (dict, list)):
                self.walk(value, path=self._join(path, key), page=page, depth=depth + 1)
            else:
                rendered = _scalar(value)
                if rendered:
                    leaves.append((key, rendered))
        if leaves:
            text = FIELD_SEPARATOR.join(f"{key}: {value}" for key, value in leaves)
            self._paragraph(path, text, page)

    def _sequence(self, values: list[Json], *, path: str, page: int, depth: int) -> None:
        records = [value for value in values if _is_record(value)]
        if records and len(records) == len(values):
            self._records(records, path=path, page=page)
            return
        scalars = [_scalar(value) for value in values if not isinstance(value, (dict, list))]
        if scalars:
            self._paragraph(path, FIELD_SEPARATOR.join(v for v in scalars if v), page)
        for index, value in enumerate(values, start=1):
            if isinstance(value, (dict, list)):
                self.walk(value, path=self._join(path, str(index)), page=page, depth=depth + 1)

    def _records(self, records: list[Json], *, path: str, page: int) -> None:
        """A list of objects is a table: one element per record, all sharing column names."""
        keys = _record_keys(records)
        columns = header_names(keys)  # an empty key still gets a usable column name
        table = self._join(path, "") or self._root
        for record in records:
            values = [_scalar(record.get(key, "")) for key in keys]
            element = table_row_element(
                columns, values, table=table, section=self._section(path), page=page
            )
            if element is not None:
                self._elements.add(element)

    def _paragraph(self, path: str, text: str, page: int) -> None:
        if not text:
            return
        self._elements.add(
            DocumentElement(
                kind=ElementKind.PARAGRAPH,
                text=text,
                section=self._section(path),
                page=page,
            )
        )

    @staticmethod
    def _join(path: str, key: str) -> str:
        return f"{path}.{key}" if path and key else (path or key)

    def _section(self, path: str) -> str:
        parts = [self._root, *(part for part in path.split(".") if part)]
        return SECTION_SEPARATOR.join(parts)
