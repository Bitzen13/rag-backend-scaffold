"""Splitting a run of text that is too long for one chunk.

`paragraph` mode packs words up to the token budget, the cheapest thing that works for any
script. `sentence` mode packs whole sentences and overlaps by whole sentences, so a chunk never
starts or ends mid-thought; a single sentence longer than the budget still has to be split, and
falls back to words, then to characters for text with no word breaks at all.

Splitters always return verbatim slices of the text they were given, with the offsets to prove
it, so a chunk can always be traced back to its source.
"""

import logging
from dataclasses import dataclass
from typing import Literal, Protocol

from app.chunking.sentences import sentence_spans, word_spans
from app.chunking.tokenizer import Tokenizer

logger = logging.getLogger(__name__)

ProseSplitting = Literal["paragraph", "sentence"]

Span = tuple[int, int]


@dataclass(frozen=True)
class TextPart:
    """One piece of a split: a verbatim slice of the input text with its exact token count."""

    text: str
    char_start: int
    char_end: int
    token_count: int


class ProseSplitter(Protocol):
    def split(self, text: str, budget: int) -> list[TextPart]:
        """Split `text` into parts of at most `budget` tokens each, in order."""
        ...


class _Packer:
    """Greedily packs ordered units into windows of at most `budget` tokens, with overlap."""

    def __init__(self, tokenizer: Tokenizer, overlap: int) -> None:
        self._tokenizer = tokenizer
        self._overlap = max(0, overlap)

    def pack(self, text: str, units: list[Span], budget: int) -> list[TextPart]:
        if budget < 1:
            raise ValueError("token budget must be at least 1")
        units = [span for unit in units for span in self._fit(text, unit, budget)]
        if not units:
            return []
        costs = [self._tokenizer.count(text[start:end]) for start, end in units]
        overlap = min(self._overlap, budget - 1)

        parts: list[TextPart] = []
        first = 0
        while first < len(units):
            last = first
            total = costs[first]
            while last + 1 < len(units) and total + costs[last + 1] <= budget:
                last += 1
                total += costs[last]
            # Per-unit counts are only an estimate of the assembled window: a tokenizer merges
            # across boundaries, and the whitespace between two units is not in either of them.
            # So the window is measured for real, and shrunk until it fits.
            part = self._part(text, units[first][0], units[last][1])
            while part.token_count > budget and last > first:
                last -= 1
                part = self._part(text, units[first][0], units[last][1])
            parts.append(part)
            if last + 1 >= len(units):
                break
            first = self._next_start(costs, first, last, overlap)
        return parts

    @staticmethod
    def _next_start(costs: list[int], first: int, last: int, overlap: int) -> int:
        """Where the next window starts: far enough back to overlap, far enough on to progress."""
        start = last + 1
        carried = 0
        while start - 1 > first and carried + costs[start - 1] <= overlap:
            start -= 1
            carried += costs[start]
        return start

    def _part(self, text: str, start: int, end: int) -> TextPart:
        body = text[start:end]
        return TextPart(body, start, end, self._tokenizer.count(body))

    def _fit(self, text: str, unit: Span, budget: int) -> list[Span]:
        """Break a unit that is larger than the whole budget into pieces that fit."""
        start, end = unit
        if end <= start:
            return []
        if self._tokenizer.count(text[start:end]) <= budget:
            return [unit]
        words = [(start + a, start + b) for a, b in word_spans(text[start:end])]
        if len(words) > 1:
            return [span for word in words for span in self._fit(text, word, budget)]
        return self._bisect(text, start, end, budget)

    def _bisect(self, text: str, start: int, end: int, budget: int) -> list[Span]:
        """Last resort for text with no word breaks (CJK, a long identifier, encoded data)."""
        if end - start <= 1 or self._tokenizer.count(text[start:end]) <= budget:
            return [(start, end)]
        middle = (start + end) // 2
        return self._bisect(text, start, middle, budget) + self._bisect(text, middle, end, budget)


class WordWindowSplitter:
    """Windows of whole words. Cuts fall between words but may fall mid-sentence."""

    mode: ProseSplitting = "paragraph"

    def __init__(self, tokenizer: Tokenizer, overlap: int) -> None:
        self._packer = _Packer(tokenizer, overlap)

    def split(self, text: str, budget: int) -> list[TextPart]:
        return self._packer.pack(text, word_spans(text), budget)


class SentenceWindowSplitter:
    """Windows of whole sentences, overlapping by whole sentences."""

    mode: ProseSplitting = "sentence"

    def __init__(self, tokenizer: Tokenizer, overlap: int) -> None:
        self._packer = _Packer(tokenizer, overlap)

    def split(self, text: str, budget: int) -> list[TextPart]:
        return self._packer.pack(text, sentence_spans(text), budget)


def build_prose_splitter(mode: ProseSplitting, tokenizer: Tokenizer, overlap: int) -> ProseSplitter:
    if mode == "sentence":
        return SentenceWindowSplitter(tokenizer, overlap)
    return WordWindowSplitter(tokenizer, overlap)
