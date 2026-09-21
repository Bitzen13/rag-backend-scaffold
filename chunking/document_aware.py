"""Structure-aware chunking: one algorithm over the element model, whatever the file format.

The rules are written against element kinds, never against a format, because that is what makes
one strategy enough for every document a reader can describe:

- a table row is its own chunk, carrying its table caption and column names;
- a code block stays whole;
- list items are grouped under the line that introduces them;
- a question keeps its answer, as the context every answer chunk repeats;
- transcript turns are grouped by speaker and by a time window;
- a message (email) is one chunk;
- prose paragraphs are packed together under their heading path.

Every chunk repeats the context it needs to stand on its own once it is retrieved out of the
document, and that context is counted against the chunk's token budget rather than added on top
of it. A document with no elements falls back to fixed-size windows.
"""

import logging
import re
from dataclasses import dataclass, field

from chunking.assembly import ChunkAssembler
from chunking.base import ChunkPlan, TextChunk
from chunking.fixed_size import FixedSizeChunker
from chunking.prose import ProseSplitter, WordWindowSplitter, build_prose_splitter
from chunking.tokenizer import Tokenizer
from db.models import JsonObject
from extraction.base import DocumentElement, ElementKind, ExtractedDocument

logger = logging.getLogger(__name__)

# A chunk's context line may take at most this share of the chunk's tokens; a deeper heading path
# than that is trimmed from the front, so there is always room for the content itself.
CONTEXT_MAX_FRACTION = 0.25
CONTEXT_SEPARATOR = " / "
# Where a context line may be cut when it is too long: between its parts, or between the headings
# of the path inside one of them (see extraction.readers.base.SECTION_SEPARATOR).
CONTEXT_CUTS = re.compile(r" / | > ")
# Joining the context to the body can cost a token or two that per-piece counting cannot see.
CONTEXT_JOIN_ALLOWANCE = 2
# Transcript turns by different speakers belong to one chunk only within this many seconds.
TRANSCRIPT_WINDOW_SECONDS = 120.0

CHUNK_TYPES: dict[ElementKind, str] = {
    ElementKind.PARAGRAPH: "prose",
    ElementKind.LIST_ITEM: "list",
    ElementKind.TABLE_ROW: "table_row",
    ElementKind.CODE: "code",
    ElementKind.QUESTION: "qa",
    ElementKind.ANSWER: "qa",
    ElementKind.SPEAKER_TURN: "transcript",
    ElementKind.MESSAGE: "message",
}
# Kinds whose elements never share a chunk with a neighbour.
STANDALONE = frozenset({ElementKind.TABLE_ROW, ElementKind.CODE, ElementKind.MESSAGE})
BODY_SEPARATORS: dict[ElementKind, str] = {
    ElementKind.LIST_ITEM: "\n",
    ElementKind.SPEAKER_TURN: "\n",
}
DEFAULT_BODY_SEPARATOR = "\n\n"


@dataclass
class _Group:
    """Elements that will become one chunk (or, if one element is oversized, several).

    `lead` sets the context and the budget. For a question-and-answer group the lead is the
    question, which is why it is not repeated in the body: it is the context.
    """

    lead: DocumentElement
    context: str
    budget: int  # tokens available for the body, once the context has had its share
    body: list[DocumentElement] = field(default_factory=list)
    tokens: int = 0

    @property
    def kind(self) -> ElementKind:
        return self.lead.kind

    def body_text(self) -> str:
        separator = BODY_SEPARATORS.get(self.kind, DEFAULT_BODY_SEPARATOR)
        return separator.join(_rendered(e) for e in self.body)

    def char_range(self) -> tuple[int, int]:
        elements = self.body or [self.lead]
        return elements[0].char_start, elements[-1].char_end


def _rendered(element: DocumentElement) -> str:
    """An element's text as it appears inside a chunk body."""
    if element.kind is ElementKind.SPEAKER_TURN and element.speaker:
        return f"{element.speaker}: {element.text}"
    return element.text


class DocumentAwareChunker:
    def __init__(self, plan: ChunkPlan, tokenizer: Tokenizer) -> None:
        self._plan = plan
        self._tokenizer = tokenizer
        self._splitter: ProseSplitter = build_prose_splitter(
            plan.prose_splitting, tokenizer, plan.chunk_overlap
        )
        self._words = WordWindowSplitter(tokenizer, plan.chunk_overlap)
        self._fallback = FixedSizeChunker(plan, tokenizer)
        self._max_context = max(1, int(plan.chunk_size * CONTEXT_MAX_FRACTION))

    def chunk(self, document: ExtractedDocument) -> list[TextChunk]:
        if not document.elements:
            return self._fallback.chunk(document)

        assembler = ChunkAssembler(
            self._tokenizer, self._plan.max_chunk_tokens, WordWindowSplitter(self._tokenizer, 0)
        )
        group: _Group | None = None
        question: DocumentElement | None = None  # so a split answer keeps repeating its question
        for element in document.elements:
            if element.kind is ElementKind.HEADING:
                continue  # carried as `section` on the elements beneath it
            if element.kind is ElementKind.QUESTION:
                question = element
            elif element.kind is not ElementKind.ANSWER:
                question = None

            if group is not None and self._joins(group, element):
                self._append(group, element)
                continue
            if group is not None:
                self._emit(group, assembler)
            group = self._start(element, question)
        if group is not None:
            self._emit(group, assembler)
        return assembler.chunks()

    # ---- grouping ------------------------------------------------------------------------

    def _start(self, element: DocumentElement, question: DocumentElement | None) -> _Group:
        lead = question if element.kind is ElementKind.ANSWER and question is not None else element
        context = self._fit_context(self._context_for(lead))
        overhead = self._tokenizer.count(context) + CONTEXT_JOIN_ALLOWANCE if context else 0
        group = _Group(lead=lead, context=context, budget=max(1, self._plan.chunk_size - overhead))
        if lead is element and element.kind is ElementKind.QUESTION:
            return group  # the question is the context; its answers will form the body
        self._append(group, element)
        return group

    def _append(self, group: _Group, element: DocumentElement) -> None:
        group.tokens += self._cost(group, element)
        group.body.append(element)

    def _cost(self, group: _Group, element: DocumentElement) -> int:
        """What adding an element costs the group, counting the separator it arrives with.

        Counting the separator matters: the pieces then concatenate to exactly the body text, so
        their counts add up to at least the body's own count, and a group that fits its budget by
        this measure cannot come out over it once assembled.
        """
        separator = (
            "" if not group.body else BODY_SEPARATORS.get(group.kind, DEFAULT_BODY_SEPARATOR)
        )
        return self._tokenizer.count(separator + _rendered(element))

    def _joins(self, group: _Group, element: DocumentElement) -> bool:
        """Whether an element belongs in the group being built."""
        if group.kind in STANDALONE or element.kind in STANDALONE:
            return False
        if element.kind is ElementKind.QUESTION:
            return False
        if element.kind is ElementKind.ANSWER:
            if group.kind is not ElementKind.QUESTION:
                return False
            # An empty question group takes its first answer whatever the size: the answer is then
            # split with the question repeated on each part.
            return not group.body or self._fits(group, element)
        if group.kind is not element.kind or group.lead.section != element.section:
            return False
        if element.kind is ElementKind.LIST_ITEM and group.lead.lead_in != element.lead_in:
            return False
        if element.kind is ElementKind.SPEAKER_TURN and not self._same_turn_window(group, element):
            return False
        return self._fits(group, element)

    def _fits(self, group: _Group, element: DocumentElement) -> bool:
        return group.tokens + self._cost(group, element) <= group.budget

    @staticmethod
    def _same_turn_window(group: _Group, element: DocumentElement) -> bool:
        """A chunk covers one window of the conversation: turns (of any speaker, each still
        labelled with theirs) join while the window holds, and a long gap starts a new chunk."""
        start, now = group.lead.time_seconds, element.time_seconds
        if start is None or now is None:
            return True  # a transcript with no timestamps: size is the only limit
        return now - start <= TRANSCRIPT_WINDOW_SECONDS

    # ---- context -------------------------------------------------------------------------

    @staticmethod
    def _context_for(element: DocumentElement) -> str:
        parts: list[str] = []
        if element.kind is ElementKind.TABLE_ROW:
            parts.append(element.table)
            # A table caption usually already names its section ("...in the Milky Way").
            if element.section.lower() not in element.table.lower():
                parts.append(element.section)
        else:
            parts.append(element.section)
            if element.kind is ElementKind.LIST_ITEM and element.lead_in not in element.section:
                parts.append(element.lead_in)
            elif element.kind is ElementKind.CODE and element.language:
                parts.append(f"{element.language} code")
            elif element.kind is ElementKind.QUESTION:
                parts.append(element.text)
        return CONTEXT_SEPARATOR.join(part for part in parts if part)

    def _fit_context(self, context: str) -> str:
        """Keep the context inside its share of the budget, dropping its least specific part.

        Outer parts go first, whether they are separated by the context separator or by the
        heading-path one: for "Rocket > Engines > Nozzle" the nozzle is what identifies the chunk.
        """
        if not context or self._tokenizer.count(context) <= self._max_context:
            return context
        for cut in CONTEXT_CUTS.finditer(context):
            candidate = context[cut.end() :]
            if self._tokenizer.count(candidate) <= self._max_context:
                return candidate
        # A single part still too long: keep its tail, which is the specific end of a long title.
        parts = self._words.split(context, self._max_context)
        return parts[-1].text if parts else ""

    @staticmethod
    def _compose(context: str, body: str) -> str:
        return f"{context}\n{body}" if context else body

    # ---- emitting ------------------------------------------------------------------------

    def _emit(self, group: _Group, assembler: ChunkAssembler) -> None:
        body = group.body_text()
        start, end = group.char_range()
        if not body:  # a question with no answer, or an element that cleaned down to nothing
            assembler.add(group.context, start, end, self._metadata(group))
        elif group.tokens <= group.budget:
            assembler.add(self._compose(group.context, body), start, end, self._metadata(group))
        else:
            self._emit_oversized(group, assembler)

    def _emit_oversized(self, group: _Group, assembler: ChunkAssembler) -> None:
        """Split a single oversized element, repeating the context (and a label) on every part.

        Only one element can ever be oversized: a group stops growing while it still fits, so a
        group over its budget holds exactly one element. That is what keeps each part's offsets
        exact, since a part is a slice of that element's own text.
        """
        element = group.body[0]
        # A label identifies which row or speaker a part belongs to, and so is repeated on every
        # part and comes out of the budget rather than on top of it (measured at its longest, so
        # the count cannot grow once the number of parts is known). Prose needs no label: its
        # heading path already says where it sits, and "(part 2/3)" is not worth embedding.
        label = self._part_label(element)
        reserved = self._tokenizer.count(f"{label} (part 999/999)\n") if label else 0
        parts = self._splitter.split(_rendered(element), max(1, group.budget - reserved))
        metadata = self._metadata(group)
        for number, part in enumerate(parts, start=1):
            body = f"{label} (part {number}/{len(parts)})\n{part.text}" if label else part.text
            assembler.add(
                self._compose(group.context, body),
                element.char_start + part.char_start,
                element.char_start + part.char_end,
                metadata,
            )

    @staticmethod
    def _part_label(element: DocumentElement) -> str:
        """What every part of a split element repeats, so a part is still identifiable."""
        if element.kind is ElementKind.TABLE_ROW and element.fields:
            return element.fields[0][1]
        if element.kind is ElementKind.SPEAKER_TURN:
            return element.speaker
        return ""

    @staticmethod
    def _metadata(group: _Group) -> JsonObject:
        lead = group.lead
        elements = (lead, *group.body)
        metadata: JsonObject = {"chunk_type": CHUNK_TYPES[lead.kind]}
        if lead.kind is ElementKind.TABLE_ROW:
            metadata["table"] = lead.table
        metadata["section"] = lead.section
        metadata["page"] = lead.page
        if lead.kind is ElementKind.TABLE_ROW:
            metadata["row_name"] = lead.fields[0][1] if lead.fields else ""
        if lead.language:
            metadata["language"] = lead.language
        speakers = sorted({e.speaker for e in elements if e.speaker})
        if speakers:
            metadata["speakers"] = speakers
        times = [e.time_seconds for e in elements if e.time_seconds is not None]
        if times:
            metadata["time_start"] = min(times)
            metadata["time_end"] = max(times)
        if lead.line_start:
            metadata["line_start"] = lead.line_start
            metadata["line_end"] = max(e.line_end for e in elements)
        return metadata
