"""Source code reader: one element per top-level definition.

Splitting code on a fixed window cuts functions in half, which makes a retrieved chunk useless.
This splits on the only structural boundary every language agrees on - a line that starts in
column zero - and names each block from the definition keyword it starts with, so a chunk carries
the name of the function or class it holds. It is not a parser and does not need to be: the unit
is the top-level block, and the name is context, not semantics.
"""

import logging
import re
from pathlib import Path

from app.db.models import JsonObject
from app.extraction.base import DocumentElement, ElementKind
from app.extraction.elements import ElementList
from app.extraction.readers.base import ReadResult
from app.extraction.text import (
    decode_bytes,
    normalize_newlines,
    squash,
    strip_invalid_characters,
    tidy_block,
)

logger = logging.getLogger(__name__)

LANGUAGES: dict[str, str] = {
    ".py": "python",
    ".pyi": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".tsx": "typescript",
    ".java": "java",
    ".kt": "kotlin",
    ".kts": "kotlin",
    ".go": "go",
    ".rs": "rust",
    ".rb": "ruby",
    ".php": "php",
    ".c": "c",
    ".h": "c",
    ".cc": "cpp",
    ".cpp": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".cs": "csharp",
    ".swift": "swift",
    ".scala": "scala",
    ".sh": "shell",
    ".bash": "shell",
    ".zsh": "shell",
    ".ps1": "powershell",
    ".sql": "sql",
    ".css": "css",
    ".scss": "scss",
    ".less": "less",
    ".lua": "lua",
    ".pl": "perl",
    ".pm": "perl",
    ".r": "r",
    ".jl": "julia",
    ".dart": "dart",
    ".ex": "elixir",
    ".exs": "elixir",
    ".tf": "terraform",
}

# The keyword that introduces a named block, in the languages above. Modifiers before it
# (public, static, async, export, pub, ...) are skipped by the leading group.
_DEFINITION = re.compile(
    r"^(?:(?:export|default|public|private|protected|internal|static|final|abstract|async|"
    r"unsafe|extern|pub|open|override|inline|const|declare|readonly)\s+)*"
    r"(?P<keyword>def|class|function|func|fn|struct|interface|type|enum|impl|trait|module|"
    r"namespace|record|object|package|sub|procedure|create\s+(?:table|view|function|index))\s+"
    r"(?P<name>[\w.:<>$-]+)",
    re.IGNORECASE,
)
# A bare function definition with no keyword at all: "name(args) {" as in C, or a shell function.
_BARE_DEFINITION = re.compile(r"^(?P<name>[\w:~*&.\[\]]+)\s*\([^;]*\)\s*(?:\{|:)?\s*$")
_COMMENT_START = ("#", "//", "/*", "*", "--", '"""', "'''", "<!--", "%", ";")


def language_of(path: Path) -> str:
    return LANGUAGES.get(path.suffix.lower(), path.suffix.lstrip(".").lower())


def block_name(lines: list[str]) -> str:
    """The name of the definition a block holds, or "" when it is not a named definition."""
    for line in lines[:5]:
        stripped = line.strip()
        if not stripped or stripped.startswith(_COMMENT_START):
            continue
        match = _DEFINITION.match(stripped)
        if match is not None:
            keyword = squash(match.group("keyword")).lower()
            return f"{keyword} {_clean_name(match.group('name'))}".strip()
        bare = _BARE_DEFINITION.match(stripped)
        if bare is not None:
            return _clean_name(bare.group("name"))
        return ""
    return ""


def _clean_name(name: str) -> str:
    """The identifier alone, without the punctuation that followed it on the line."""
    return name.rstrip(":;,(){}[]<>")


def _is_comment(line: str) -> bool:
    return line.strip().startswith(_COMMENT_START)


class CodeReader:
    """Reads source files in the languages listed in LANGUAGES."""

    version = 1  # bump to force re-extraction of documents this reader read

    @property
    def name(self) -> str:
        return "code"

    @property
    def extensions(self) -> tuple[str, ...]:
        return tuple(LANGUAGES)

    def read(self, path: Path) -> ReadResult:
        raw, encoding = decode_bytes(path.read_bytes())
        text, removed = strip_invalid_characters(raw)
        if removed:
            logger.warning("%s: removed %d invalid characters", path.name, removed)
        lines = normalize_newlines(text).split("\n")
        language = language_of(path)
        elements = ElementList()
        named = 0
        for first, last in top_level_blocks(lines):
            block = lines[first : last + 1]
            name = block_name(block)
            named += bool(name)
            body = tidy_block("\n".join(block))
            if not body:
                continue
            elements.add(
                DocumentElement(
                    kind=ElementKind.CODE,
                    text=body,
                    section=name,
                    language=language,
                    line_start=first + 1,
                    line_end=last + 1,
                )
            )
        document_text, placed = elements.build()
        metadata: JsonObject = {
            "encoding": encoding,
            "language": language,
            "lines": len(lines),
            "definitions": named,
        }
        return ReadResult(document_text, placed, metadata)


def top_level_blocks(lines: list[str]) -> list[tuple[int, int]]:
    """Line ranges of the file's top-level blocks, as [first, last] inclusive indices.

    A block starts at a line in column zero that begins a named definition, or at the first
    column-zero line after a blank one. Everything in between - a function's body, a comment
    attached to it, a closing brace - stays with the block it belongs to. Statements that are not
    definitions (imports, constants) accumulate into one block rather than one chunk each.
    """
    starts: list[int] = []
    previous_blank = True
    for index, line in enumerate(lines):
        if not line.strip():
            previous_blank = True
            continue
        at_margin = not line.startswith((" ", "\t"))
        if (
            at_margin
            and (block_name([line]) or previous_blank or not starts)
            and (not starts or starts[-1] != index)
        ):
            starts.append(index)
        previous_blank = False

    # Merge a run of unnamed blocks (imports, constants, a stray closing brace) into one.
    merged: list[int] = []
    for start in starts:
        if merged and not block_name([lines[start]]) and not block_name([lines[merged[-1]]]):
            continue
        merged.append(start)
    # A comment block directly above a definition introduces it: keep them together.
    kept: list[int] = []
    for position, start in enumerate(merged):
        following = merged[position + 1] if position + 1 < len(merged) else len(lines)
        block = [line for line in lines[start:following] if line.strip()]
        if block and all(_is_comment(line) for line in block) and position + 1 < len(merged):
            continue
        kept.append(start)

    return [
        (start, (kept[position + 1] if position + 1 < len(kept) else len(lines)) - 1)
        for position, start in enumerate(kept)
    ]
