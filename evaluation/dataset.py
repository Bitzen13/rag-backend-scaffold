"""The evaluation set: questions paired with the passage that should be retrieved for them.

A case names a passage by a piece of its text rather than by chunk id, because chunk ids change
every time chunking changes - which is exactly when the evaluation needs to stay comparable.
"""

from dataclasses import dataclass
from pathlib import Path

import yaml

from extraction.text import squash


class DatasetError(ValueError):
    """The evaluation file is missing, malformed, or empty."""


@dataclass(frozen=True)
class EvalCase:
    """One question and what counts as having retrieved the right thing for it."""

    question: str
    expect: tuple[str, ...]  # a chunk containing any of these passages is a hit
    source_uri: str | None = None  # when set, the hit must also come from this source
    note: str = ""

    def matches(self, chunk_text: str, source_uri: str) -> bool:
        if self.source_uri is not None and self.source_uri != source_uri:
            return False
        haystack = normalize(chunk_text)
        return any(normalize(passage) in haystack for passage in self.expect)


def normalize(text: str) -> str:
    """Whitespace and case are never what a case is testing."""
    return squash(text).casefold()


def load_dataset(path: Path) -> tuple[EvalCase, ...]:
    """Load cases from a YAML file with a top-level `cases:` list."""
    try:
        raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, yaml.YAMLError) as exc:
        raise DatasetError(f"could not read the evaluation set {path}: {exc}") from exc
    if not isinstance(raw, dict) or not isinstance(raw.get("cases"), list):
        raise DatasetError(f"{path} must be a YAML mapping with a top-level 'cases:' list")
    cases = tuple(_case(entry, path, number) for number, entry in enumerate(raw["cases"], start=1))
    if not cases:
        raise DatasetError(f"{path} contains no cases")
    return cases


def _case(entry: object, path: Path, number: int) -> EvalCase:
    where = f"{path} case {number}"
    if not isinstance(entry, dict):
        raise DatasetError(f"{where}: each case must be a mapping")
    question = str(entry.get("question", "")).strip()
    if not question:
        raise DatasetError(f"{where}: 'question' is required")
    expect = entry.get("expect")
    passages = [expect] if isinstance(expect, str) else expect
    if not isinstance(passages, list) or not passages:
        raise DatasetError(f"{where}: 'expect' must be a passage, or a list of them")
    source_uri = entry.get("source_uri")
    unknown = set(entry) - {"question", "expect", "source_uri", "note"}
    if unknown:
        raise DatasetError(f"{where}: unknown keys {sorted(unknown)}")
    return EvalCase(
        question=question,
        expect=tuple(str(passage) for passage in passages),
        source_uri=str(source_uri) if source_uri else None,
        note=str(entry.get("note", "")),
    )
