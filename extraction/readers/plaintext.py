"""Plain text reader: light structure detection, and the fallback for unknown extensions.

Plain text has no markup to rely on, so structure is inferred, and only where the evidence is
strong enough that ordinary prose cannot be mistaken for it:

- a heading is an underlined line, an all-capitals short line, or a numbered section title that
  stands alone (never a numbered list item, which has a sibling on the next line);
- a transcript is recognised for the document as a whole - several "Speaker: said" lines and at
  least two distinct speakers - so that a stray "Note: ..." line in prose is never a turn;
- "Q:" and "A:" markers make question and answer elements, which chunking keeps together.

Anything not recognised stays a paragraph, which is always a safe answer.
"""

import logging
import re
from pathlib import Path

from extraction.base import DocumentElement, ElementKind
from extraction.elements import ElementList
from extraction.readers.base import ReadResult, SectionStack
from extraction.text import (
    decode_bytes,
    normalize_newlines,
    squash,
    strip_invalid_characters,
    tidy_block,
)

logger = logging.getLogger(__name__)

HEADING_MAX_WORDS = 12
NUMBERED_HEADING_MAX_WORDS = 8
UNDERLINE_CHARACTERS = "=-~^\"*+#'`"
MIN_UNDERLINE_LENGTH = 3
# A document is read as a transcript only with this much evidence.
TRANSCRIPT_MIN_TURNS = 3
TRANSCRIPT_MIN_SPEAKERS = 2
TRANSCRIPT_MIN_LINE_FRACTION = 0.3

_FENCE = re.compile(r"^\s*(`{3,}|~{3,})\s*([^\s`]*)")
_UNDERLINE = re.compile(rf"^\s*([{re.escape(UNDERLINE_CHARACTERS)}])\1{{2,}}\s*$")
_BULLET = re.compile(r"^(\s*)([-*+•◦▪‣·]|\(?\d{1,3}[.)]|\(?[a-zA-Z][.)])\s+(.*)$")
_NUMBERED_HEADING = re.compile(r"^\s*(\d+(?:\.\d+)*)\.?\s+(\S.*)$")
_SPEAKER = re.compile(
    r"^\s*(?:[\[(]?(?P<time>\d{1,3}:\d{2}(?::\d{2})?)[\])]?\s*[-–]?\s*)?"
    r"(?P<who>[^\s:][^:]{0,40}?)\s*:\s+(?P<said>\S.*)$"
)
_QUESTION = re.compile(r"^\s*(?:Q|Question)\s*[:.)]\s*(\S.*)$", re.IGNORECASE)
_ANSWER = re.compile(r"^\s*(?:A|Answer)\s*[:.)]\s*(\S.*)$", re.IGNORECASE)
_SENTENCE_END = (".", "!", "?", ",", ";", ":")
_SPEAKER_MAX_NAME_WORDS = 5


def parse_timestamp(value: str | None) -> float | None:
    """Seconds from "mm:ss" or "hh:mm:ss"."""
    if not value:
        return None
    parts = [int(part) for part in value.split(":")]
    if len(parts) == 2:
        return parts[0] * 60 + parts[1]
    return parts[0] * 3600 + parts[1] * 60 + parts[2]


def speaker_match(line: str) -> re.Match[str] | None:
    """A "Speaker: said" line, where the name is short enough to be a name."""
    match = _SPEAKER.match(line)
    if match is None:
        return None
    who = match.group("who").strip()
    if not who or len(who.split()) > _SPEAKER_MAX_NAME_WORDS or who.endswith(_SENTENCE_END):
        return None
    return match


def looks_like_transcript(lines: list[str]) -> bool:
    filled = [line for line in lines if line.strip()]
    if not filled:
        return False
    matches = [speaker_match(line) for line in filled]
    turns = [m for m in matches if m is not None]
    speakers = {m.group("who").strip() for m in turns}
    return (
        len(turns) >= TRANSCRIPT_MIN_TURNS
        and len(speakers) >= TRANSCRIPT_MIN_SPEAKERS
        and len(turns) >= TRANSCRIPT_MIN_LINE_FRACTION * len(filled)
    )


class PlainTextReader:
    """Reads .txt and anything else with no dedicated reader."""

    @property
    def name(self) -> str:
        return "text"

    @property
    def extensions(self) -> tuple[str, ...]:
        return (".txt", ".text", ".log", ".rst")

    def read(self, path: Path) -> ReadResult:
        raw, encoding = decode_bytes(path.read_bytes())
        text, removed = strip_invalid_characters(raw)
        if removed:
            logger.warning("%s: removed %d invalid characters (e.g. NUL)", path.name, removed)
        lines = normalize_newlines(text).split("\n")
        parser = _Parser(lines)
        document_text, elements = parser.parse().build()
        return ReadResult(
            document_text,
            elements,
            {"encoding": encoding, "structure": parser.structure, "lines": len(lines)},
        )


class _Parser:
    def __init__(self, lines: list[str]) -> None:
        self._lines = lines
        self._transcript = looks_like_transcript(lines)
        self._elements = ElementList()
        self._sections = SectionStack()
        self._heading_styles: dict[tuple[object, ...], int] = {}
        self._paragraph: list[str] = []
        self._paragraph_line = 0
        self._last_paragraph = ""
        self._list_lead_in = ""
        self._in_answer = False

    @property
    def structure(self) -> str:
        return "transcript" if self._transcript else "prose"

    def parse(self) -> ElementList:
        index = 0
        while index < len(self._lines):
            if not self._lines[index].strip():
                self._flush()
                index += 1
                continue
            handlers = (
                (self._turn, self._heading)
                if self._transcript
                else (self._fence, self._heading, self._qa, self._list_item)
            )
            for handler in handlers:
                consumed = handler(index)
                if consumed:
                    index += consumed
                    break
            else:
                if not self._paragraph:
                    self._paragraph_line = index + 1
                self._paragraph.append(self._lines[index].strip())
                index += 1
        self._flush()
        return self._elements

    def _add(
        self,
        kind: ElementKind,
        text: str,
        first: int,
        last: int,
        *,
        language: str = "",
        lead_in: str = "",
        speaker: str = "",
        time_seconds: float | None = None,
    ) -> None:
        self._elements.add(
            DocumentElement(
                kind=kind,
                text=text,
                section=self._sections.path(),
                language=language,
                lead_in=lead_in,
                speaker=speaker,
                time_seconds=time_seconds,
                line_start=first,
                line_end=last,
            )
        )

    # ---- transcripts ---------------------------------------------------------------------

    def _turn(self, index: int) -> int:
        match = speaker_match(self._lines[index])
        if match is None:
            return 0  # a wrapped continuation line: let it join the previous paragraph
        self._flush()
        said = [match.group("said").strip()]
        cursor = index + 1
        while cursor < len(self._lines):
            following = self._lines[cursor]
            if not following.strip() or speaker_match(following) is not None:
                break
            said.append(following.strip())
            cursor += 1
        self._add(
            ElementKind.SPEAKER_TURN,
            squash(" ".join(said)),
            index + 1,
            cursor,
            speaker=match.group("who").strip(),
            time_seconds=parse_timestamp(match.group("time")),
        )
        return cursor - index

    # ---- questions and answers -----------------------------------------------------------

    def _qa(self, index: int) -> int:
        """A "Q:" or "A:" line and the lines that continue it.

        Handled per line rather than per paragraph, because a question and its answer are usually
        written on consecutive lines with no blank line between them.
        """
        line = self._lines[index]
        question = _QUESTION.match(line)
        answer = None if question is not None else _ANSWER.match(line)
        match = question or answer
        if match is None:
            return 0
        self._flush()
        parts = [match.group(1).strip()]
        cursor = index + 1
        # Wrapped lines continue the question or answer, but only until it reads as finished:
        # an answer usually follows its question on the very next line, unmarked.
        while cursor < len(self._lines) and not parts[-1].endswith((".", "?", "!")):
            following = self._lines[cursor]
            if not following.strip() or self._starts_block(following):
                break
            parts.append(following.strip())
            cursor += 1
        kind = ElementKind.QUESTION if question is not None else ElementKind.ANSWER
        self._in_answer = question is not None
        self._add(kind, squash(" ".join(parts)), index + 1, cursor)
        return cursor - index

    @staticmethod
    def _starts_block(line: str) -> bool:
        return bool(
            _QUESTION.match(line)
            or _ANSWER.match(line)
            or _BULLET.match(line)
            or _FENCE.match(line)
            or _UNDERLINE.match(line)
        )

    # ---- prose ---------------------------------------------------------------------------

    def _fence(self, index: int) -> int:
        match = _FENCE.match(self._lines[index])
        if match is None:
            return 0
        self._flush()
        marker, language = match.groups()
        body: list[str] = []
        cursor = index + 1
        while cursor < len(self._lines):
            candidate = self._lines[cursor].strip()
            if candidate and set(candidate) == {marker[0]} and len(candidate) >= len(marker):
                cursor += 1
                break
            body.append(self._lines[cursor])
            cursor += 1
        code = tidy_block("\n".join(body))
        if code:
            self._add(ElementKind.CODE, code, index + 1, cursor, language=language.lower())
        return cursor - index

    def _heading(self, index: int) -> int:
        line = self._lines[index]
        underline = _UNDERLINE.match(line)
        if underline is not None:
            if len(self._paragraph) == 1 and len(line.strip()) >= MIN_UNDERLINE_LENGTH:
                self._push(("underline", underline.group(1)), self._paragraph[0], index)
                self._paragraph.clear()
                self._paragraph_line = 0
            return 1
        if self._paragraph:
            return 0  # a heading starts a block; mid-paragraph lines are prose
        stripped = line.strip()
        if (
            stripped.isupper()
            and any(character.isalpha() for character in stripped)
            and len(stripped.split()) <= HEADING_MAX_WORDS
        ):
            self._push(("capitals",), stripped, index)
            return 1
        numbered = _NUMBERED_HEADING.match(line)
        if numbered is not None and self._is_numbered_heading(numbered, index):
            self._push(("numbered", numbered.group(1).count(".")), stripped, index)
            return 1
        return 0

    def _is_numbered_heading(self, match: re.Match[str], index: int) -> bool:
        """A numbered line is a section title only if it stands alone.

        A numbered list item has a sibling touching it: another item on the very next line, or on
        the line directly above. A section title stands apart, is short, and has no full stop.
        """
        following = self._lines[index + 1] if index + 1 < len(self._lines) else ""
        above = self._lines[index - 1] if index > 0 else ""
        return (
            not following.strip()
            and _BULLET.match(above) is None
            and len(match.group(2).split()) <= NUMBERED_HEADING_MAX_WORDS
            and not match.group(2).endswith(_SENTENCE_END)
        )

    def _level(self, style: tuple[object, ...]) -> int:
        """Heading levels follow the order their styles first appear, as reST underlines do.

        Plain text has no levels of its own, so the document's own conventions supply them: the
        first style seen is level 1, the next distinct style is level 2, and so on. A numbered
        style counts its dots, so "2." and "2.1" are different styles and nest correctly.
        """
        if style not in self._heading_styles:
            self._heading_styles[style] = len(self._heading_styles) + 1
        return self._heading_styles[style]

    def _push(self, style: tuple[object, ...], title: str, index: int) -> None:
        title = squash(title)
        if not title:
            return
        self._sections.push(self._level(style), title)
        self._list_lead_in = ""
        self._in_answer = False
        self._add(ElementKind.HEADING, title, index + 1, index + 1)

    def _list_item(self, index: int) -> int:
        match = _BULLET.match(self._lines[index])
        if match is None:
            return 0
        # The line introducing a list is the pending paragraph, or the one just above a blank
        # line, when it ends in a colon.
        pending = squash(" ".join(self._paragraph)) or self._last_paragraph
        lead_in = self._list_lead_in or (pending if pending.endswith(":") else "")
        self._flush()
        self._list_lead_in = lead_in
        indent, _, first = match.groups()
        parts = [first.strip()]
        cursor = index + 1
        while cursor < len(self._lines):
            candidate = self._lines[cursor]
            if not candidate.strip() or _BULLET.match(candidate):
                break
            if len(candidate) - len(candidate.lstrip()) <= len(indent):
                break
            parts.append(candidate.strip())
            cursor += 1
        self._add(
            ElementKind.LIST_ITEM, squash(" ".join(parts)), index + 1, cursor, lead_in=lead_in
        )
        return cursor - index

    def _flush(self) -> None:
        if not self._paragraph:
            return
        text = squash(" ".join(self._paragraph))
        first, last = self._paragraph_line, self._paragraph_line + len(self._paragraph) - 1
        self._paragraph.clear()
        self._paragraph_line = 0
        self._list_lead_in = ""
        if not text:
            return
        question = _QUESTION.match(text)
        if question is not None:
            self._in_answer = True
            self._add(ElementKind.QUESTION, question.group(1).strip(), first, last)
            return
        answer = _ANSWER.match(text)
        if answer is not None:
            self._in_answer = False
            self._add(ElementKind.ANSWER, answer.group(1).strip(), first, last)
            return
        if self._in_answer:
            self._in_answer = False
            self._add(ElementKind.ANSWER, text, first, last)
            return
        self._last_paragraph = text
        self._add(ElementKind.PARAGRAPH, text, first, last)
