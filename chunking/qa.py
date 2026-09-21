"""Quality checks on a document's chunks, so chunking can be judged before anything is embedded.

Chunking is where most retrieval problems are created, and they are invisible once the chunks are
vectors. These checks look for the defects that actually cause bad answers: chunks so small they
carry no information, chunks over the size that was asked for, duplicates that crowd out other
results, and cuts in the wrong place - a chunk that starts mid-sentence or ends mid-word.

Every check is defined against the source text and offsets rather than against a format, so it
applies to every reader and both strategies. `scripts/ingest.py --dry-run` prints the result.
"""

import re
import statistics
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass

from chunking.base import ChunkPlan, TextChunk
from db.models import JsonObject
from extraction.base import ExtractedDocument

# A prose chunk smaller than this share of the target size is suspiciously small.
UNDERSIZED_FRACTION = 0.1
UNDERSIZED_FLOOR = 20  # tokens; below this a chunk is too small whatever the target size
# Chunk kinds that are legitimately short: a table row or a list of two items is not a defect.
SHORT_BY_NATURE = frozenset({"table_row", "code", "list", "message"})
URL_MAX_FRACTION = 0.5  # more of the chunk than this being a URL leaves nothing to retrieve on
DUPLICATE_SAMPLE = 60  # characters of a duplicate shown in the report

_URL = re.compile(r"\b(?:https?://|www\.)\S+|\b\S+\.(?:com|org|net|io|gov|edu)/\S*")
_SENTENCE_END = (".", "!", "?", "…", "。", "！", "？", ":", ";")
_CLOSERS = " \"'”’)]}"

ISSUE_LABELS: dict[str, str] = {
    "over_cap": "over the hard token cap",
    "oversized": "larger than chunk_size",
    "empty": "no content of its own",
    "undersized": "very small",
    "duplicate": "exact duplicate of another chunk",
    "mid_sentence_start": "starts mid-sentence",
    "mid_word_end": "ends mid-word",
    "mostly_url": "mostly a URL",
}
# Issues that mean a chunk is broken, as opposed to merely worth a look.
DEFECTS = frozenset({"over_cap", "empty", "oversized"})


@dataclass(frozen=True)
class ChunkIssue:
    kind: str
    chunk_index: int
    detail: str

    def describe(self) -> str:
        label = ISSUE_LABELS.get(self.kind, self.kind)
        return (
            f"chunk {self.chunk_index}: {label} ({self.detail})"
            if self.detail
            else (f"chunk {self.chunk_index}: {label}")
        )


@dataclass(frozen=True)
class SizeStats:
    count: int
    minimum: int
    p10: int
    median: int
    mean: int
    p90: int
    maximum: int

    @staticmethod
    def of(sizes: Sequence[int]) -> "SizeStats":
        if not sizes:
            return SizeStats(0, 0, 0, 0, 0, 0, 0)
        ordered = sorted(sizes)
        return SizeStats(
            count=len(ordered),
            minimum=ordered[0],
            p10=ordered[max(0, int(0.1 * (len(ordered) - 1)))],
            median=int(statistics.median(ordered)),
            mean=int(statistics.fmean(ordered)),
            p90=ordered[int(0.9 * (len(ordered) - 1))],
            maximum=ordered[-1],
        )

    def describe(self) -> str:
        return (
            f"tokens min/p10/median/mean/p90/max = {self.minimum}/{self.p10}/{self.median}/"
            f"{self.mean}/{self.p90}/{self.maximum}"
        )

    def as_json(self) -> JsonObject:
        return {
            "count": self.count,
            "min": self.minimum,
            "p10": self.p10,
            "median": self.median,
            "mean": self.mean,
            "p90": self.p90,
            "max": self.maximum,
        }


@dataclass(frozen=True)
class ChunkQaReport:
    source_uri: str
    plan: ChunkPlan
    token_counts: tuple[int, ...]
    types: dict[str, int]
    issues: tuple[ChunkIssue, ...]

    @property
    def sizes(self) -> SizeStats:
        return SizeStats.of(self.token_counts)

    @property
    def issue_counts(self) -> dict[str, int]:
        return dict(sorted(Counter(issue.kind for issue in self.issues).items()))

    @property
    def defect_count(self) -> int:
        return sum(1 for issue in self.issues if issue.kind in DEFECTS)

    def as_json(self) -> JsonObject:
        return {
            "source_uri": self.source_uri,
            "plan": self.plan.as_metadata(),
            "sizes": self.sizes.as_json(),
            "types": self.types,
            "issues": self.issue_counts,
        }


def _ends_sentence(text: str) -> bool:
    return text.rstrip(_CLOSERS).endswith(_SENTENCE_END)


def _starts_mid_sentence(text: str, start: int) -> bool:
    """True when the chunk begins inside a sentence that started before it."""
    before = text[:start].rstrip()
    if not before:
        return False
    first = text[start : start + 1]
    return bool(first) and first.islower() and first.isascii() and not _ends_sentence(before)


def _ends_mid_word(text: str, end: int) -> bool:
    return end < len(text) and text[end - 1 : end].isalnum() and text[end : end + 1].isalnum()


def _url_fraction(text: str) -> float:
    if not text:
        return 0.0
    return sum(len(match.group()) for match in _URL.finditer(text)) / len(text)


def review_chunks(
    source_uri: str, plan: ChunkPlan, document: ExtractedDocument, chunks: Sequence[TextChunk]
) -> ChunkQaReport:
    """Check a document's chunks and report what is wrong with them."""
    issues: list[ChunkIssue] = []
    undersized = max(UNDERSIZED_FLOOR, int(plan.chunk_size * UNDERSIZED_FRACTION))
    text = document.text
    seen: dict[str, int] = {}

    for chunk in chunks:
        kind = str(chunk.metadata.get("chunk_type", "text"))
        source = text[chunk.char_start : chunk.char_end]
        if chunk.token_count > plan.max_chunk_tokens:
            issues.append(ChunkIssue("over_cap", chunk.index, f"{chunk.token_count} tokens"))
        elif chunk.token_count > plan.chunk_size:
            issues.append(
                ChunkIssue(
                    "oversized", chunk.index, f"{chunk.token_count} > {plan.chunk_size} tokens"
                )
            )
        if not source.strip():
            issues.append(ChunkIssue("empty", chunk.index, "context only"))
        elif chunk.token_count < undersized and kind not in SHORT_BY_NATURE:
            issues.append(ChunkIssue("undersized", chunk.index, f"{chunk.token_count} tokens"))
        key = " ".join(chunk.text.split()).lower()
        if key in seen:
            issues.append(
                ChunkIssue(
                    "duplicate", chunk.index, f"same as chunk {seen[key]}: {key[:DUPLICATE_SAMPLE]}"
                )
            )
        else:
            seen[key] = chunk.index
        if _starts_mid_sentence(text, chunk.char_start):
            issues.append(ChunkIssue("mid_sentence_start", chunk.index, source[:40]))
        if _ends_mid_word(text, chunk.char_end):
            issues.append(ChunkIssue("mid_word_end", chunk.index, source[-40:]))
        fraction = _url_fraction(chunk.text)
        if fraction > URL_MAX_FRACTION:
            issues.append(ChunkIssue("mostly_url", chunk.index, f"{fraction:.0%} of the text"))

    return ChunkQaReport(
        source_uri=source_uri,
        plan=plan,
        token_counts=tuple(chunk.token_count for chunk in chunks),
        types=dict(
            sorted(
                Counter(str(chunk.metadata.get("chunk_type", "text")) for chunk in chunks).items()
            )
        ),
        issues=tuple(issues),
    )


def format_report(report: ChunkQaReport, *, examples: int = 3) -> str:
    """The report for one document, as lines for a terminal or a file."""
    lines = [
        f"{report.source_uri}: {report.sizes.count} chunks [{report.plan.describe()}]",
        f"  {report.sizes.describe()}",
        "  types: " + (", ".join(f"{k}={v}" for k, v in report.types.items()) or "none"),
    ]
    if not report.issues:
        lines.append("  no issues found")
        return "\n".join(lines)
    lines.append("  issues: " + ", ".join(f"{k}={v}" for k, v in report.issue_counts.items()))
    by_kind: dict[str, list[ChunkIssue]] = {}
    for issue in report.issues:
        by_kind.setdefault(issue.kind, []).append(issue)
    for kind, found in by_kind.items():
        for issue in found[:examples]:
            lines.append(f"    {issue.describe()}")
        if len(found) > examples:
            lines.append(f"    ... and {len(found) - examples} more {kind}")
    return "\n".join(lines)


def format_summary(reports: Sequence[ChunkQaReport]) -> str:
    """One corpus-wide summary, which is what a baseline is compared on."""
    if not reports:
        return "no documents chunked"
    sizes = SizeStats.of([size for report in reports for size in report.token_counts])
    counts: Counter[str] = Counter()
    for report in reports:
        counts.update(report.issue_counts)
    lines = [
        f"{len(reports)} documents, {sizes.count} chunks",
        f"  {sizes.describe()}",
    ]
    if counts:
        lines.append("  issues: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        defects = sum(count for kind, count in counts.items() if kind in DEFECTS)
        lines.append(f"  defects (empty or oversized chunks): {defects}")
    else:
        lines.append("  no issues found")
    return "\n".join(lines)


def reports_as_json(reports: Sequence[ChunkQaReport]) -> JsonObject:
    counts: Counter[str] = Counter()
    for report in reports:
        counts.update(report.issue_counts)
    return {
        "documents": [report.as_json() for report in reports],
        "totals": {
            "documents": len(reports),
            "chunks": sum(report.sizes.count for report in reports),
            "issues": dict(sorted(counts.items())),
        },
    }
