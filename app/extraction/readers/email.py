"""Email reader: one element per message, for .eml files and mbox archives.

A message is the retrievable unit, so each one becomes a single element with its headers rendered
into the text (a question about who sent what, and when, is answerable only if those are part of
the text that gets embedded). Quoted replies and signatures are removed, because in a thread they
would otherwise be stored once per message.
"""

import logging
import mailbox
import re
from collections.abc import Iterator
from email import policy
from email.header import decode_header, make_header
from email.message import EmailMessage, Message
from email.parser import BytesParser
from email.utils import getaddresses
from pathlib import Path

from app.db.models import JsonObject
from app.extraction.base import DocumentElement, ElementKind
from app.extraction.elements import ElementList
from app.extraction.readers.base import ReaderError, ReadResult, require
from app.extraction.text import squash, strip_invalid_characters, tidy_block

logger = logging.getLogger(__name__)

RENDERED_HEADERS = ("From", "To", "Cc", "Date", "Subject")

# The start of a quoted reply. Everything from here on repeats an earlier message.
_QUOTE_START = re.compile(
    r"^\s*(?:>|On .{0,120}\bwrote:|-{2,}\s*Original Message\s*-{2,}|_{5,}"
    r"|From:\s.+\bSent:|Le .{0,120}\ba écrit\s*:|Am .{0,120}schrieb\s)",
    re.IGNORECASE,
)
_SIGNATURE = re.compile(r"^\s*--\s*$")


def header_text(message: Message, name: str) -> str:
    """A header value with any MIME word encoding resolved."""
    raw = message.get(name)
    if not raw:
        return ""
    try:
        return squash(str(make_header(decode_header(str(raw)))))
    except (UnicodeDecodeError, ValueError):
        return squash(str(raw))


def sender_name(message: Message) -> str:
    """The sender as a person: their display name, or their address if that is all there is."""
    addresses = getaddresses([header_text(message, "From")])
    if not addresses:
        return ""
    name, address = addresses[0]
    return name or address


def strip_quotes(body: str) -> str:
    """Drop the quoted reply and the signature below the message's own text."""
    kept: list[str] = []
    for line in body.splitlines():
        if _QUOTE_START.match(line) or _SIGNATURE.match(line):
            break
        kept.append(line)
    return tidy_block("\n".join(kept)) or tidy_block(body)


def message_body(message: Message) -> str:
    """The message's plain-text body, converting from HTML only if there is no text part."""
    if isinstance(message, EmailMessage):
        try:
            part = message.get_body(preferencelist=("plain", "html"))
        except (AttributeError, KeyError):
            part = None
        if part is not None:
            content = part.get_content()
            text = content if isinstance(content, str) else str(content)
            if part.get_content_subtype() == "html":
                return _html_to_text(text)
            return text
    payload = message.get_payload(decode=True)
    if isinstance(payload, bytes):
        charset = message.get_content_charset() or "utf-8"
        return payload.decode(charset, errors="replace")
    return str(message.get_payload())


def _html_to_text(html: str) -> str:
    bs4 = require("bs4", "beautifulsoup4")
    return str(bs4.BeautifulSoup(html, "html.parser").get_text("\n", strip=True))


def attachment_names(message: Message) -> list[str]:
    return [
        squash(str(name))
        for part in message.walk()
        if (name := part.get_filename()) and part.get_content_maintype() != "multipart"
    ]


def render_message(message: Message) -> str:
    """Headers plus body, as one block of text that answers questions about the message."""
    lines = [
        f"{name}: {value}" for name in RENDERED_HEADERS if (value := header_text(message, name))
    ]
    body, removed = strip_invalid_characters(strip_quotes(message_body(message)))
    if removed:
        logger.debug("removed %d invalid characters from a message body", removed)
    return tidy_block("\n".join(lines) + "\n\n" + body)


class EmailReader:
    """Reads a single message (.eml, .msg-as-mime) or an mbox archive."""

    version = 1  # bump to force re-extraction of documents this reader read

    @property
    def name(self) -> str:
        return "email"

    @property
    def extensions(self) -> tuple[str, ...]:
        return (".eml", ".mbox")

    def read(self, path: Path) -> ReadResult:
        elements = ElementList()
        subjects: list[str] = []
        attachments: list[str] = []
        count = 0
        for number, message in enumerate(self._messages(path), start=1):
            subject = header_text(message, "Subject")
            elements.add(
                DocumentElement(
                    kind=ElementKind.MESSAGE,
                    text=render_message(message),
                    page=number,
                    section=subject,
                    speaker=sender_name(message),
                )
            )
            count += 1
            if subject and subject not in subjects:
                subjects.append(subject)
            attachments.extend(attachment_names(message))
        document_text, placed = elements.build()
        metadata: JsonObject = {"messages": count, "subjects": subjects}
        if attachments:
            metadata["attachments"] = sorted(set(attachments))
        return ReadResult(document_text, placed, metadata)

    @staticmethod
    def _messages(path: Path) -> Iterator[Message]:
        if path.suffix.lower() == ".mbox":
            try:
                archive = mailbox.mbox(str(path), create=False)
            except (OSError, mailbox.Error) as exc:
                raise ReaderError(f"could not open {path.name} as an mbox: {exc}") from exc
            try:
                yield from archive
            finally:
                archive.close()
            return
        with path.open("rb") as handle:
            yield BytesParser(policy=policy.default).parse(handle)
