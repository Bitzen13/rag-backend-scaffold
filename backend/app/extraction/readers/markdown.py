"""Markdown reader: headings, paragraphs, lists, fenced code and pipe tables.

Hand-written rather than pulled from a Markdown library, because what is needed here is document
structure, not HTML: a heading's level, a list's lead-in line, a table's header row.
"""

import logging
import re
from pathlib import Path
from typing import Any

import yaml

from app.extraction import yaml_safe
from app.extraction.base import DocumentElement, ElementKind
from app.extraction.elements import ElementList
from app.extraction.readers.base import ReadResult, SectionStack
from app.extraction.tables import TableReader, looks_like_header
from app.extraction.text import (
    decode_bytes,
    normalize_newlines,
    squash,
    strip_invalid_characters,
    tidy_block,
)

logger = logging.getLogger(__name__)

CAPTION_MAX_WORDS = 20

_FRONT_MATTER = re.compile(r"^---[ \t]*\n(.*?)\n---[ \t]*(?:\n|$)", re.DOTALL)
_FENCE = re.compile(r"^(\s*)(`{3,}|~{3,})\s*([^\s`]*)")
_ATX = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")
_SETEXT = re.compile(r"^(=+|-{2,})\s*$")
_BULLET = re.compile(r"^(\s*)([-*+]|\d{1,3}[.)]|[a-zA-Z][.)])\s+(.*)$")
_TABLE_DELIMITER = re.compile(r"^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)+\|?\s*$")
_RULE = re.compile(r"^\s*([-*_])(\s*\1){2,}\s*$")
_IMAGE = re.compile(r"!\[([^\]]*)\]\(([^)\s]*)[^)]*\)")
_LINK = re.compile(r"\[([^\]]+)\]\(([^)\s]*)[^)]*\)")
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)


def render_inline(text: str) -> str:
    """Keep the information in links and images, drop the syntax around it."""
    text = _IMAGE.sub(lambda m: m.group(1) or Path(m.group(2)).name, text)
    text = _LINK.sub(lambda m: f"{m.group(1)} ({m.group(2)})" if m.group(2) else m.group(1), text)
    return text


def looks_like_caption(text: str) -> bool:
    """A short line with no full stop reads as a table's caption rather than as prose."""
    return bool(text) and len(text.split()) <= CAPTION_MAX_WORDS and not text.endswith(".")


def split_table_row(line: str) -> list[str]:
    stripped = line.strip()
    if stripped.startswith("|"):
        stripped = stripped[1:]
    if stripped.endswith("|"):
        stripped = stripped[:-1]
    return [cell.strip() for cell in stripped.split("|")]


class MarkdownReader:
    # bump to force re-extraction of documents this reader read
    # 2: front-matter YAML aliases are no longer expanded (extraction/yaml_safe.py)
    version = 2

    @property
    def name(self) -> str:
        return "markdown"

    @property
    def extensions(self) -> tuple[str, ...]:
        return (".md", ".markdown", ".mdown", ".mkd")

    def read(self, path: Path) -> ReadResult:
        raw, encoding = decode_bytes(path.read_bytes())
        text, removed = strip_invalid_characters(raw)
        if removed:
            logger.warning("%s: removed %d invalid characters", path.name, removed)
        # Before anything line-based: a file written on Windows has CRLF endings, and every
        # pattern here (front matter, fences, underlines) is written against "\n".
        text = normalize_newlines(text)
        metadata, body, offset = self._front_matter(_HTML_COMMENT.sub("", text))
        elements = _Parser(body, offset).parse()
        document_text, placed = elements.build()
        return ReadResult(document_text, placed, {**metadata, "encoding": encoding})

    @staticmethod
    def _front_matter(text: str) -> tuple[dict[str, Any], str, int]:
        """YAML front matter becomes document metadata rather than retrievable text."""
        match = _FRONT_MATTER.match(text)
        if match is None:
            return {}, text, 1
        try:
            loaded = yaml_safe.load(match.group(1))
        except yaml.YAMLError as exc:
            logger.warning("ignoring unparseable front matter: %s", exc)
            loaded = None
        parsed = {str(k): v for k, v in loaded.items()} if isinstance(loaded, dict) else {}
        offset = text.count("\n", 0, match.end()) + 1
        return ({"front_matter": parsed} if parsed else {}), text[match.end() :], offset


class _Parser:
    """One pass over the lines, holding the little state Markdown structure needs."""

    def __init__(self, text: str, line_offset: int) -> None:
        self._lines = text.split("\n")  # the reader normalised them already
        self._offset = line_offset
        self._elements = ElementList()
        self._sections = SectionStack()
        self._paragraph: list[str] = []
        self._paragraph_line = 0
        self._last_paragraph = ""
        self._list_lead_in = ""

    def parse(self) -> ElementList:
        index = 0
        while index < len(self._lines):
            line = self._lines[index]
            if not line.strip():
                self._flush_paragraph()
                index += 1
                continue
            for handler in (self._fence, self._heading, self._table, self._list_item):
                consumed = handler(index)
                if consumed:
                    index += consumed
                    break
            else:
                self._paragraph_line = self._paragraph_line or self._number(index)
                self._paragraph.append(line.lstrip("> ").rstrip())
                index += 1
        self._flush_paragraph()
        return self._elements

    def _number(self, index: int) -> int:
        return self._offset + index

    def _add(
        self,
        kind: ElementKind,
        text: str,
        first: int,
        last: int,
        *,
        language: str = "",
        lead_in: str = "",
    ) -> None:
        self._elements.add(
            DocumentElement(
                kind=kind,
                text=text,
                section=self._sections.path(),
                language=language,
                lead_in=lead_in,
                line_start=first,
                line_end=last,
            )
        )

    # ---- blocks --------------------------------------------------------------------------

    def _fence(self, index: int) -> int:
        match = _FENCE.match(self._lines[index])
        if match is None:
            return 0
        self._flush_paragraph()
        indent, marker, language = match.groups()
        body: list[str] = []
        cursor = index + 1
        while cursor < len(self._lines):
            candidate = self._lines[cursor].strip()
            if candidate.startswith(marker[0] * len(marker)) and set(candidate) == {marker[0]}:
                cursor += 1
                break
            body.append(self._lines[cursor][len(indent) :] if indent else self._lines[cursor])
            cursor += 1
        code = tidy_block("\n".join(body))
        if code:
            self._add(
                ElementKind.CODE,
                code,
                self._number(index),
                self._number(cursor - 1),
                language=language.lower(),
            )
        return cursor - index

    def _heading(self, index: int) -> int:
        line = self._lines[index]
        match = _ATX.match(line)
        if match is not None:
            self._flush_paragraph()
            self._push_heading(len(match.group(1)), render_inline(match.group(2)), index)
            return 1
        # An underline turns the single pending line above it into a heading. "---" is both an
        # underline and a horizontal rule; with a line pending it is the former.
        if _SETEXT.match(line):
            if len(self._paragraph) == 1:
                title = self._paragraph[0]
                self._paragraph.clear()
                self._paragraph_line = 0
                self._push_heading(1 if line.strip().startswith("=") else 2, title, index)
            return 1
        return 1 if _RULE.match(line) else 0

    def _push_heading(self, level: int, title: str, index: int) -> None:
        title = squash(title)
        if not title:
            return
        self._sections.push(level, title)
        self._last_paragraph = ""
        self._list_lead_in = ""
        self._add(ElementKind.HEADING, title, self._number(index), self._number(index))

    def _table(self, index: int) -> int:
        line = self._lines[index]
        following = self._lines[index + 1] if index + 1 < len(self._lines) else ""
        if "|" not in line or not _TABLE_DELIMITER.match(following):
            return 0
        header = split_table_row(line)
        pending = squash(" ".join(self._paragraph)) if self._paragraph else self._last_paragraph
        caption = pending if looks_like_caption(pending) else ""
        self._flush_paragraph()
        table = TableReader(table=caption, section=self._sections.path())
        if looks_like_header(header):
            table.set_headers(header)
        cursor = index + 2
        while cursor < len(self._lines) and "|" in self._lines[cursor]:
            row = table.add(
                [render_inline(cell) for cell in split_table_row(self._lines[cursor])],
                line=self._number(cursor),
            )
            if row is not None:
                self._elements.add(row)
            cursor += 1
        return cursor - index

    def _list_item(self, index: int) -> int:
        match = _BULLET.match(self._lines[index])
        if match is None:
            return 0
        lead_in = self._list_lead_in or (
            self._last_paragraph if self._paragraph_line == 0 else " ".join(self._paragraph)
        )
        self._flush_paragraph()
        self._list_lead_in = lead_in if lead_in.endswith(":") else ""
        indent, _, first = match.groups()
        parts = [first.strip()]
        cursor = index + 1
        # Continuation lines of the same item are indented further and are not new items.
        while cursor < len(self._lines):
            candidate = self._lines[cursor]
            if not candidate.strip() or _BULLET.match(candidate):
                break
            if len(candidate) - len(candidate.lstrip()) <= len(indent):
                break
            parts.append(candidate.strip())
            cursor += 1
        self._add(
            ElementKind.LIST_ITEM,
            render_inline(squash(" ".join(parts))),
            self._number(index),
            self._number(cursor - 1),
            lead_in=self._list_lead_in,
        )
        return cursor - index

    def _flush_paragraph(self) -> None:
        if not self._paragraph:
            return
        text = render_inline(squash(" ".join(self._paragraph)))
        first, last = self._paragraph_line, self._paragraph_line + len(self._paragraph) - 1
        self._paragraph.clear()
        self._paragraph_line = 0
        self._list_lead_in = ""
        if not text:
            return
        self._last_paragraph = text
        self._add(ElementKind.PARAGRAPH, text, first, last)
