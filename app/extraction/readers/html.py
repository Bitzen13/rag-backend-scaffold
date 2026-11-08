"""HTML reader: the main content of a page, as sections, lists, code and tables.

Two jobs beyond mapping tags to elements. First, boilerplate removal: navigation, sidebars,
cookie banners and footers are stripped, and the container holding the real content is chosen by
how much prose it holds, rather than by any site-specific selector. Second, structure: a heading
level comes from its tag, a table's header row from its `th` cells, and a code block keeps its
line breaks.
"""

import logging
import re
from pathlib import Path
from typing import Any

from app.db.models import JsonObject
from app.extraction.base import DocumentElement, ElementKind
from app.extraction.elements import ElementList
from app.extraction.readers.base import ReadResult, SectionStack, require
from app.extraction.tables import TableReader
from app.extraction.text import decode_bytes, squash, strip_invalid_characters, tidy_block

logger = logging.getLogger(__name__)

# Removed outright: these never hold the content of a page.
BOILERPLATE_TAGS = (
    "script",
    "style",
    "noscript",
    "template",
    "svg",
    "iframe",
    "form",
    "button",
    "select",
    "nav",
    "footer",
    "header",
    "aside",
)
# Removed when a class, id or role says what the block is for.
BOILERPLATE_PATTERN = re.compile(
    r"(^|[-_ ])(nav|navbar|menu|sidebar|side-bar|footer|header|masthead|breadcrumb|cookie|consent"
    r"|banner|advert|ads?|promo|social|share|subscribe|newsletter|related|recommend|comment"
    r"|pagination|paginate|toc|skip-link|search)([-_ ]|$)",
    re.IGNORECASE,
)
CONTENT_CANDIDATES = ("main", "article", "[role=main]", "#content", "#main", ".content", ".post")
# A candidate container must hold this share of the page's prose to be preferred over the body.
CONTENT_MIN_SHARE = 0.5
CAPTION_MAX_WORDS = 20

HEADINGS = ("h1", "h2", "h3", "h4", "h5", "h6")
BLOCK_TAGS = (*HEADINGS, "p", "blockquote", "li", "dt", "dd", "pre", "table", "figcaption")
_LANGUAGE_CLASS = re.compile(r"(?:language|lang|highlight|brush)[-:]([\w+#]+)", re.IGNORECASE)


def _attribute_words(tag: Any) -> str:
    classes = tag.get("class") or []
    parts = [" ".join(classes) if isinstance(classes, list) else str(classes)]
    for name in ("id", "role", "aria-label", "data-testid"):
        value = tag.get(name)
        if value:
            parts.append(str(value))
    return " ".join(parts)


def strip_boilerplate(soup: Any) -> None:
    """Remove the parts of a page that are furniture rather than content."""
    for tag in soup.find_all(BOILERPLATE_TAGS):
        if not getattr(tag, "decomposed", False):
            tag.decompose()
    for tag in soup.find_all(True):
        if getattr(tag, "decomposed", False) or tag.name in BLOCK_TAGS:
            continue  # never drop a content block on the strength of its class name
        if BOILERPLATE_PATTERN.search(_attribute_words(tag)):
            tag.decompose()


def strip_comments(soup: Any) -> None:
    """Remove comments, doctypes and processing instructions: markup, never content.

    `get_text` skips them already, but a comment is a `str` subclass, so code that walks a tag's
    children by hand (`_direct_text`) would otherwise read one as text.
    """
    from bs4 import Comment, Declaration, Doctype, ProcessingInstruction

    for node in soup.find_all(
        string=lambda s: isinstance(s, (Comment, Declaration, Doctype, ProcessingInstruction))
    ):
        node.extract()


def _prose_length(tag: Any) -> int:
    return sum(len(block.get_text(" ", strip=True)) for block in tag.find_all(("p", "li", "td")))


def main_content(soup: Any) -> Any:
    """The container holding the page's content, chosen by how much prose it holds."""
    body = soup.body or soup
    total = max(1, _prose_length(body))
    for selector in CONTENT_CANDIDATES:
        for candidate in body.select(selector):
            if _prose_length(candidate) >= CONTENT_MIN_SHARE * total:
                return candidate
    best, best_score = body, total
    for candidate in body.find_all(("div", "section")):
        score = _prose_length(candidate)
        if score > CONTENT_MIN_SHARE * total and score <= best_score:
            best, best_score = candidate, score  # the smallest container still holding the prose
    return best


def _language_of(tag: Any) -> str:
    for attribute in (_attribute_words(tag), _attribute_words(tag.find("code") or tag)):
        match = _LANGUAGE_CLASS.search(attribute)
        if match is not None:
            return match.group(1).lower()
    return ""


def _direct_text(tag: Any) -> str:
    """A list item's own text, without the nested list hanging off it."""
    parts = [
        child if isinstance(child, str) else child.get_text(" ", strip=True)
        for child in tag.children
        if isinstance(child, str) or child.name not in ("ul", "ol", "dl")
    ]
    return squash(" ".join(parts))


class HtmlReader:
    # bump to force re-extraction of documents this reader read
    # 2: comments are no longer extracted as text (one inside an <li> used to be)
    version = 2

    @property
    def name(self) -> str:
        return "html"

    @property
    def extensions(self) -> tuple[str, ...]:
        return (".html", ".htm", ".xhtml")

    def read(self, path: Path) -> ReadResult:
        bs4 = require("bs4", "beautifulsoup4")
        raw, encoding = decode_bytes(path.read_bytes())
        text, removed = strip_invalid_characters(raw)
        if removed:
            logger.warning("%s: removed %d invalid characters", path.name, removed)
        soup = bs4.BeautifulSoup(text, "html.parser")
        metadata = self._metadata(soup, encoding)
        strip_comments(soup)
        strip_boilerplate(soup)
        root = main_content(soup)
        document_text, elements = _Walker(root).walk().build()
        return ReadResult(document_text, elements, metadata)

    @staticmethod
    def _metadata(soup: Any, encoding: str) -> JsonObject:
        metadata: JsonObject = {"encoding": encoding}
        if soup.title is not None and soup.title.string:
            metadata["title"] = squash(str(soup.title.string))
        html = soup.find("html")
        if html is not None and html.get("lang"):
            metadata["language"] = str(html.get("lang"))
        description = soup.find("meta", attrs={"name": "description"})
        if description is not None and description.get("content"):
            metadata["description"] = squash(str(description.get("content")))
        return metadata


class _Walker:
    """Walks the content in document order, emitting one element per content block."""

    def __init__(self, root: Any) -> None:
        self._root = root
        self._elements = ElementList()
        self._sections = SectionStack()
        self._consumed: set[int] = set()
        self._last_text = ""
        self._term = ""

    def walk(self) -> ElementList:
        for tag in self._root.find_all(BLOCK_TAGS):
            if any(id(parent) in self._consumed for parent in tag.parents):
                continue  # already emitted as part of a table or a code block
            name = str(tag.name)
            if name in HEADINGS:
                self._heading(tag, int(name[1]))
            elif name == "li":
                self._list_item(tag)
            elif name == "dt":
                self._term = squash(tag.get_text(" ", strip=True))
            elif name == "dd":
                self._definition(tag)
            elif name == "pre":
                self._code(tag)
            elif name == "table":
                self._table(tag)
            else:
                self._paragraph(tag)
        return self._elements

    def _add(
        self,
        kind: ElementKind,
        text: str,
        *,
        language: str = "",
        lead_in: str = "",
    ) -> None:
        if not text:
            return
        self._elements.add(
            DocumentElement(
                kind=kind,
                text=text,
                section=self._sections.path(),
                language=language,
                lead_in=lead_in,
            )
        )

    def _heading(self, tag: Any, level: int) -> None:
        title = squash(tag.get_text(" ", strip=True))
        if not title:
            return
        self._sections.push(level, title)
        self._last_text = ""
        self._add(ElementKind.HEADING, title)

    def _paragraph(self, tag: Any) -> None:
        text = squash(tag.get_text(" ", strip=True))
        self._last_text = text
        self._add(ElementKind.PARAGRAPH, text)

    def _list_item(self, tag: Any) -> None:
        # A nested item is introduced by the item it hangs off, not by the paragraph above the
        # whole list.
        parent = tag.find_parent("li")
        if parent is not None:
            self._add(ElementKind.LIST_ITEM, _direct_text(tag), lead_in=_direct_text(parent))
            return
        lead_in = self._last_text if self._last_text.endswith(":") else ""
        self._add(ElementKind.LIST_ITEM, _direct_text(tag), lead_in=lead_in)

    def _definition(self, tag: Any) -> None:
        """A definition list reads best as "term: definition" in one element."""
        definition = squash(tag.get_text(" ", strip=True))
        self._add(
            ElementKind.PARAGRAPH, f"{self._term}: {definition}" if self._term else definition
        )

    def _code(self, tag: Any) -> None:
        self._consumed.add(id(tag))
        self._add(ElementKind.CODE, tidy_block(tag.get_text()), language=_language_of(tag))

    def _table(self, tag: Any) -> None:
        self._consumed.add(id(tag))
        caption_tag = tag.find("caption")
        caption = squash(caption_tag.get_text(" ", strip=True)) if caption_tag else ""
        if not caption and len(self._last_text.split()) <= CAPTION_MAX_WORDS:
            caption = self._last_text
        table = TableReader(table=caption, section=self._sections.path())
        rows = tag.find_all("tr")
        if rows and rows[0].find("th") is not None:
            table.set_headers([squash(c.get_text(" ", strip=True)) for c in rows[0].find_all("th")])
            rows = rows[1:]
        for row in rows:
            cells = row.find_all(("td", "th"))
            if not cells:
                continue
            element = table.add([squash(c.get_text(" ", strip=True)) for c in cells])
            if element is not None:
                self._elements.add(element)
