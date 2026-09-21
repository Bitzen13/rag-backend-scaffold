"""Checking that the evaluation set is answerable at all.

A missed case has two possible causes: retrieval did not find the passage, or no chunk contains
the passage in the first place - a typo in the case, or text the reader never produced. Only the
first is a retrieval result. This re-chunks the corpus and reports the cases no chunk could
satisfy, which needs no database and no API key.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from chunking.base import ChunkingStrategyError
from chunking.factory import ConfiguredChunkers
from evaluation.dataset import EvalCase
from extraction.base import Extractor
from settings import ChunkingStrategy

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DatasetCheck:
    """Which cases the corpus can satisfy, and which it cannot."""

    unsatisfiable: tuple[EvalCase, ...]
    unknown_sources: tuple[str, ...]
    chunks: int
    documents: int

    @property
    def ok(self) -> bool:
        return not self.unsatisfiable and not self.unknown_sources

    def describe(self) -> str:
        lines = [
            f"checked {len(self.unsatisfiable)} unsatisfiable case(s) against "
            f"{self.chunks} chunks from {self.documents} documents"
        ]
        for case in self.unsatisfiable:
            lines.append(f"  no chunk contains any of {list(case.expect)} ({case.question!r})")
        for uri in self.unknown_sources:
            lines.append(f"  no such source in the corpus: {uri}")
        if self.ok:
            lines = [
                f"every case is satisfiable ({self.chunks} chunks from {self.documents} documents)"
            ]
        return "\n".join(lines)


def check_dataset(
    cases: Sequence[EvalCase], extractor: Extractor, chunkers: ConfiguredChunkers
) -> DatasetCheck:
    """Chunk the corpus and report the cases whose expected passage is in no chunk."""
    satisfied: set[int] = set()
    sources: set[str] = set()
    chunks = 0
    documents = 0
    for document in extractor.extract():
        uri = document.source_uri
        sources.add(uri)
        try:
            strategy: ChunkingStrategy = chunkers.strategy_for(uri)
        except ChunkingStrategyError as exc:
            logger.warning("%s", exc)
            continue
        logger.debug("checking %s (%s)", uri, strategy)
        documents += 1
        for chunk in chunkers.for_document(uri).chunk(document):
            chunks += 1
            for index, case in enumerate(cases):
                if index not in satisfied and case.matches(chunk.text, uri):
                    satisfied.add(index)
    return DatasetCheck(
        unsatisfiable=tuple(case for index, case in enumerate(cases) if index not in satisfied),
        unknown_sources=tuple(
            sorted({case.source_uri for case in cases if case.source_uri} - sources)
        ),
        chunks=chunks,
        documents=documents,
    )
