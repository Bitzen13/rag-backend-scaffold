"""The retrieval evaluation harness: matching, metrics, baselines, and the dataset check.

The harness is driven with a fake retriever, so the metrics themselves are what is under test,
not the database or an embedding provider.
"""

import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest

from chunking.factory import ConfiguredChunkers
from evaluation import baseline as baselines
from evaluation.check import check_dataset
from evaluation.dataset import DatasetError, EvalCase, load_dataset
from evaluation.harness import EvalRun, RetrievalHarness, format_run
from evaluation.metrics import CaseResult, mean_reciprocal_rank, recall_at, summarize
from extraction.base import ExtractedDocument, SourceCandidate
from retrieval.retriever import RetrievedChunk
from settings import ChunkingConfig
from vector_store.base import VectorFilter


class FakeRetriever:
    """Returns a prepared list of (source_uri, text) per question, in rank order."""

    def __init__(self, answers: dict[str, list[tuple[str, str]]]) -> None:
        self._answers = answers
        self.questions: list[str] = []

    async def retrieve(
        self, question: str, filters: VectorFilter | None = None
    ) -> list[RetrievedChunk]:
        self.questions.append(question)
        return [
            RetrievedChunk(chunk_id=uuid.uuid4(), source_uri=uri, text=text)
            for uri, text in self._answers.get(question, [])
        ]


def harness(answers: dict[str, list[tuple[str, str]]], top_k: int = 5) -> RetrievalHarness:
    return RetrievalHarness(FakeRetriever(answers), top_k=top_k)  # type: ignore[arg-type]


# --- matching --------------------------------------------------------------------------------


def test_a_passage_matches_regardless_of_whitespace_and_case() -> None:
    case = EvalCase(question="q", expect=("VY Canis Majoris",))
    assert case.matches("Star name:   vy canis\n majoris | radius", "any.pdf")


def test_any_of_the_expected_passages_counts_as_a_hit() -> None:
    case = EvalCase(question="q", expect=("first option", "second option"))
    assert case.matches("text with the second option in it", "a.pdf")


def test_a_case_pinned_to_a_source_does_not_match_another_one() -> None:
    case = EvalCase(question="q", expect=("shared text",), source_uri="right.pdf")
    assert case.matches("shared text", "right.pdf")
    assert not case.matches("shared text", "wrong.pdf")


# --- metrics ---------------------------------------------------------------------------------


def results(*ranks: int | None) -> list[CaseResult]:
    return [CaseResult(question=f"q{i}", rank=rank, retrieved=()) for i, rank in enumerate(ranks)]


def test_recall_counts_the_cases_found_within_k() -> None:
    found = results(1, 3, None, 5)
    assert recall_at(found, 1) == 0.25
    assert recall_at(found, 3) == 0.5
    assert recall_at(found, 5) == 0.75
    assert recall_at([], 5) == 0.0


def test_mrr_rewards_a_hit_nearer_the_top() -> None:
    assert mean_reciprocal_rank(results(1)) == 1.0
    assert mean_reciprocal_rank(results(2)) == 0.5
    assert mean_reciprocal_rank(results(None)) == 0.0
    assert mean_reciprocal_rank(results(1, None)) == 0.5


def test_the_summary_reports_only_cut_offs_within_top_k() -> None:
    metrics = summarize(results(1, 2, None), top_k=3)
    assert sorted(metrics.recall_at) == [1, 3]
    assert metrics.cases == 3
    assert "MRR=" in metrics.describe()


# --- the harness -----------------------------------------------------------------------------


async def test_the_rank_is_the_position_of_the_first_matching_chunk() -> None:
    cases = [EvalCase(question="how big?", expect=("VY Canis Majoris",))]
    run = await harness(
        {
            "how big?": [
                ("other.pdf", "something else entirely"),
                ("stars.pdf", "Star name: VY Canis Majoris | radius"),
            ]
        }
    ).run(cases)
    assert run.results[0].rank == 2
    assert run.metrics.recall_at[1] == 0.0
    assert run.metrics.recall_at[3] == 1.0


async def test_a_question_that_retrieves_nothing_is_a_miss() -> None:
    cases = [EvalCase(question="unknown?", expect=("never appears",))]
    run = await harness({}).run(cases)
    assert run.results[0].rank is None
    assert run.misses == run.results
    assert "1 case(s) missed" in format_run(run)


async def test_a_run_where_everything_is_found_says_so() -> None:
    cases = [EvalCase(question="q", expect=("found",))]
    run = await harness({"q": [("a.pdf", "found it")]}).run(cases)
    assert run.metrics.mrr == 1.0
    assert "every case retrieved its expected passage" in format_run(run)


async def test_the_run_can_be_serialised_for_a_record() -> None:
    cases = [EvalCase(question="q", expect=("found",))]
    payload = (await harness({"q": [("a.pdf", "found it")]}).run(cases)).as_json()
    assert payload["top_k"] == 5
    assert isinstance(payload["cases"], list)


# --- baselines -------------------------------------------------------------------------------


async def run_with(ranks: Sequence[int | None]) -> EvalRun:
    cases = [EvalCase(question=f"q{i}", expect=("hit",)) for i in range(len(ranks))]
    answers: dict[str, list[tuple[str, str]]] = {}
    for index, rank in enumerate(ranks):
        chunks = [("a.pdf", "filler") for _ in range(5)]
        if rank is not None:
            chunks[rank - 1] = ("a.pdf", "hit")
        answers[f"q{index}"] = chunks
    return await harness(answers).run(cases)


async def test_a_baseline_round_trips_through_a_file(tmp_path: Path) -> None:
    run = await run_with([1, 2])
    path = tmp_path / "baseline.json"
    baselines.save(baselines.from_run(run, dataset="questions.yaml", label="before"), path)
    loaded = baselines.load(path)
    assert loaded.label == "before"
    assert loaded.metrics.mrr == pytest.approx(run.metrics.mrr)


async def test_an_improvement_is_reported_as_one(tmp_path: Path) -> None:
    before = baselines.from_run(await run_with([3, None]), dataset="d")
    comparison = baselines.compare(before, await run_with([1, 1]))
    assert not comparison.regressed
    assert "MRR" in comparison.improved
    assert "improved" in comparison.describe()


async def test_a_regression_is_named(tmp_path: Path) -> None:
    before = baselines.from_run(await run_with([1, 1]), dataset="d")
    comparison = baselines.compare(before, await run_with([None, 4]))
    assert "MRR" in comparison.regressed
    assert "REGRESSED" in comparison.describe()


async def test_an_unchanged_run_is_not_a_regression() -> None:
    before = baselines.from_run(await run_with([1, 2]), dataset="d")
    comparison = baselines.compare(before, await run_with([1, 2]))
    assert not comparison.regressed
    assert "unchanged" in comparison.describe()


def test_a_file_that_is_not_a_baseline_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "junk.json"
    path.write_text('{"something": 1}', encoding="utf-8")
    with pytest.raises(baselines.BaselineError, match="not a retrieval baseline"):
        baselines.load(path)


# --- the dataset file ------------------------------------------------------------------------


def write_dataset(tmp_path: Path, body: str) -> Path:
    path = tmp_path / "questions.yaml"
    path.write_text(body, encoding="utf-8")
    return path


def test_the_shipped_evaluation_set_loads() -> None:
    """Whatever questions.yaml holds must parse — it is replaced per project, so no count here."""
    cases = load_dataset(Path("evaluation/questions.yaml"))
    assert cases
    assert all(case.question and case.expect for case in cases)


def test_a_single_expected_passage_may_be_written_as_a_string(tmp_path: Path) -> None:
    path = write_dataset(tmp_path, "cases:\n  - question: q?\n    expect: one passage\n")
    (case,) = load_dataset(path)
    assert case.expect == ("one passage",)


@pytest.mark.parametrize(
    ("body", "message"),
    [
        ("cases: []\n", "no cases"),
        ("not_cases: []\n", "top-level 'cases:'"),
        ("cases:\n  - expect: [x]\n", "'question' is required"),
        ("cases:\n  - question: q?\n", "'expect' must be"),
        ("cases:\n  - question: q?\n    expect: [x]\n    typo: 1\n", "unknown keys"),
    ],
)
def test_a_malformed_evaluation_set_says_what_is_wrong(
    tmp_path: Path, body: str, message: str
) -> None:
    with pytest.raises(DatasetError, match=message):
        load_dataset(write_dataset(tmp_path, body))


def test_a_missing_evaluation_set_is_reported(tmp_path: Path) -> None:
    with pytest.raises(DatasetError, match="could not read"):
        load_dataset(tmp_path / "nope.yaml")


# --- checking the dataset against the corpus -------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class FakeCandidate(SourceCandidate):
    document: ExtractedDocument

    def read(self) -> ExtractedDocument:
        return self.document


class FakeExtractor:
    source_type = "file"

    def __init__(self, documents: list[ExtractedDocument]) -> None:
        self._documents = documents

    def candidates(self) -> list[FakeCandidate]:
        return [
            FakeCandidate(
                source_type=d.source_type,
                source_uri=d.source_uri,
                source_hash=d.source_uri,
                document=d,
            )
            for d in self._documents
        ]


def chunkers_for(uri: str) -> ConfiguredChunkers:
    config = ChunkingConfig.model_validate(
        {"chunk_size": 100, "chunk_overlap": 10, "documents": {uri: "fixed_size"}}
    )
    return ConfiguredChunkers(
        config, max_chunk_tokens=8000, embedding_model="text-embedding-3-small"
    )


def test_the_check_confirms_a_satisfiable_case() -> None:
    document = ExtractedDocument("file", "a.txt", "The bus provides physical structure.")
    result = check_dataset(
        [EvalCase(question="q", expect=("bus provides physical structure",))],
        FakeExtractor([document]),
        chunkers_for("a.txt"),
    )
    assert result.ok
    assert "every case is satisfiable" in result.describe()


def test_the_check_names_a_case_no_chunk_can_satisfy() -> None:
    document = ExtractedDocument("file", "a.txt", "Some other text.")
    result = check_dataset(
        [EvalCase(question="q", expect=("a passage with a typo in it",))],
        FakeExtractor([document]),
        chunkers_for("a.txt"),
    )
    assert not result.ok
    assert "no chunk contains" in result.describe()


def test_the_check_names_a_source_that_is_not_in_the_corpus() -> None:
    document = ExtractedDocument("file", "a.txt", "Some text.")
    result = check_dataset(
        [EvalCase(question="q", expect=("Some text",), source_uri="missing.pdf")],
        FakeExtractor([document]),
        chunkers_for("a.txt"),
    )
    assert result.unknown_sources == ("missing.pdf",)
