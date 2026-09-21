"""Running the evaluation set through the real retrieval path.

The harness calls the same `Retriever` the API uses, so what it measures is what a consumer will
get: the question is embedded, the vector search and the typo-tolerant keyword search both run,
and the merged result is what gets scored. Nothing about chunking or retrieval is stubbed - only
the LLM is left out, since generation is not what is being measured.
"""

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from db.models import JsonObject
from evaluation.dataset import EvalCase
from evaluation.metrics import CaseResult, Metrics, summarize
from retrieval.retriever import Retriever
from vector_store.base import VectorFilter

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class EvalRun:
    results: tuple[CaseResult, ...]
    metrics: Metrics
    top_k: int

    @property
    def misses(self) -> tuple[CaseResult, ...]:
        return tuple(result for result in self.results if not result.hit)

    def as_json(self) -> JsonObject:
        return {
            "top_k": self.top_k,
            "metrics": self.metrics.as_json(),
            "cases": [
                {
                    "question": result.question,
                    "rank": result.rank,
                    "retrieved": list(result.retrieved),
                }
                for result in self.results
            ],
        }


class RetrievalHarness:
    def __init__(self, retriever: Retriever, top_k: int) -> None:
        self._retriever = retriever
        self._top_k = top_k

    async def run(self, cases: Sequence[EvalCase], filters: VectorFilter | None = None) -> EvalRun:
        results = [await self._case(case, filters) for case in cases]
        return EvalRun(tuple(results), summarize(results, self._top_k), self._top_k)

    async def _case(self, case: EvalCase, filters: VectorFilter | None) -> CaseResult:
        chunks = await self._retriever.retrieve(case.question, filters)
        rank: int | None = None
        for position, chunk in enumerate(chunks, start=1):
            if case.matches(chunk.text, chunk.source_uri):
                rank = position
                break
        logger.debug("%r: %d chunks retrieved, first match at %s", case.question, len(chunks), rank)
        return CaseResult(
            question=case.question,
            rank=rank,
            retrieved=tuple(chunk.source_uri for chunk in chunks),
            note=case.note,
        )


def format_run(run: EvalRun, *, misses: int = 10) -> str:
    """The run as lines for a terminal: the metrics, then the questions that found nothing."""
    lines = [run.metrics.describe()]
    if not run.misses:
        lines.append("every case retrieved its expected passage")
        return "\n".join(lines)
    lines.append(f"{len(run.misses)} case(s) missed:")
    for result in run.misses[:misses]:
        found = ", ".join(dict.fromkeys(result.retrieved)) or "nothing"
        lines.append(f"  {result.question!r} -> retrieved from: {found}")
    if len(run.misses) > misses:
        lines.append(f"  ... and {len(run.misses) - misses} more")
    return "\n".join(lines)
