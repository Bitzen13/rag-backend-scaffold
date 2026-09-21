"""Retrieval metrics: how often the right passage came back, and how high up.

recall@k answers "did retrieval find it at all within k results", which is what decides whether
the LLM can possibly answer. MRR answers "how near the top was it", which is what decides whether
the answer survives a small top_k. Both are reported, because a change can improve one and hurt
the other.
"""

from collections.abc import Sequence
from dataclasses import dataclass

from db.models import JsonObject

# The cut-offs reported by default; any above the retriever's top_k are dropped.
REPORTED_K = (1, 3, 5, 10)


@dataclass(frozen=True)
class CaseResult:
    """What retrieval returned for one question."""

    question: str
    rank: int | None  # 1-based position of the first chunk that matched, or None for a miss
    retrieved: tuple[str, ...]  # source_uri of each retrieved chunk, best first
    note: str = ""

    @property
    def hit(self) -> bool:
        return self.rank is not None

    def reciprocal_rank(self) -> float:
        return 0.0 if self.rank is None else 1.0 / self.rank


@dataclass(frozen=True)
class Metrics:
    cases: int
    recall_at: dict[int, float]
    mrr: float

    def describe(self) -> str:
        recalls = "  ".join(
            f"recall@{k}={value:.3f}" for k, value in sorted(self.recall_at.items())
        )
        return f"{self.cases} cases   {recalls}   MRR={self.mrr:.3f}"

    def as_json(self) -> JsonObject:
        return {
            "cases": self.cases,
            "recall_at": {str(k): round(v, 4) for k, v in sorted(self.recall_at.items())},
            "mrr": round(self.mrr, 4),
        }


def recall_at(results: Sequence[CaseResult], k: int) -> float:
    """The share of questions whose expected passage appeared in the top k results."""
    if not results:
        return 0.0
    return sum(1 for r in results if r.rank is not None and r.rank <= k) / len(results)


def mean_reciprocal_rank(results: Sequence[CaseResult]) -> float:
    if not results:
        return 0.0
    return sum(r.reciprocal_rank() for r in results) / len(results)


def summarize(results: Sequence[CaseResult], top_k: int) -> Metrics:
    cutoffs = [k for k in REPORTED_K if k <= top_k] or [top_k]
    if top_k not in cutoffs:
        cutoffs.append(top_k)
    return Metrics(
        cases=len(results),
        recall_at={k: recall_at(results, k) for k in cutoffs},
        mrr=mean_reciprocal_rank(results),
    )
