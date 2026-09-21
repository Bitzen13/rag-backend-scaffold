# Adding a Reader — supporting a new file format

A reader's only job is to describe a file as `DocumentElement`s. Chunking, embedding, storage and
retrieval are written against those elements and never against a file format, so a new format is
a new reader and nothing else. No chunker changes, no strategy per format, no config switch.

## The element model

`extraction/base.py` defines the elements every reader produces:

| Kind | What it is | Fields that matter |
|---|---|---|
| `HEADING` | a section title | `section` (the full path including this heading) |
| `PARAGRAPH` | a run of prose | `section` |
| `LIST_ITEM` | one item of a list | `lead_in` (the line introducing the list) |
| `TABLE_ROW` | one row, with its column names | `fields`, `table` (caption or sheet name) |
| `CODE` | a code block | `language` |
| `QUESTION` / `ANSWER` | a question and the answer below it | `section` |
| `SPEAKER_TURN` | one turn of a transcript | `speaker`, `time_seconds` |
| `MESSAGE` | one email or chat message | `speaker`, `section` (the subject) |

Every element also carries `page` (page, sheet or slide number — 0 when the format has none) and
`line_start` / `line_end` (1-based, 0 when not known).

The rule for choosing a kind: **pick the kind whose chunking rule is the behaviour you want**
(see `chunking-guide.md`). If none fits, `PARAGRAPH` is always safe.

Context fields are not decoration. A chunk is retrieved out of its document and has to make sense
alone, so whatever a reader does not put on the element is information the answer will not have.

## Writing the reader

Readers live in `extraction/readers/`. The contract is `extraction/readers/base.py`:

```python
class Reader(Protocol):
    @property
    def name(self) -> str: ...  # recorded in the document's metadata
    @property
    def extensions(self) -> tuple[str, ...]: ...  # lower-case, with the leading dot
    def read(self, path: Path) -> ReadResult: ...
```

A minimal reader:

```python
class RtfReader:
    @property
    def name(self) -> str:
        return "rtf"

    @property
    def extensions(self) -> tuple[str, ...]:
        return (".rtf",)

    def read(self, path: Path) -> ReadResult:
        raw, encoding = decode_bytes(path.read_bytes())  # never assume UTF-8
        text, removed = strip_invalid_characters(raw)  # NUL breaks Postgres
        if removed:
            logger.warning("%s: removed %d invalid characters", path.name, removed)
        elements = ElementList()
        for block in parse_blocks(normalize_newlines(text)):  # your format's own parsing
            elements.add(
                DocumentElement(
                    kind=ElementKind.PARAGRAPH,
                    text=squash(block.text),
                    section=self._sections.path(),
                )
            )
        document_text, placed = elements.build()
        return ReadResult(document_text, placed, {"encoding": encoding})
```

Then register it in `extraction/readers/registry.py`, in `build_registry`.

### The helpers that already exist — use them rather than re-deriving

| Need | Use |
|---|---|
| Decode a file of unknown encoding | `extraction.text.decode_bytes` |
| Strip characters Postgres cannot store | `extraction.text.strip_invalid_characters` |
| Normalise CRLF before line-based parsing | `extraction.text.normalize_newlines` |
| Collapse whitespace in prose | `extraction.text.squash` |
| Tidy a block whose line breaks matter (code) | `extraction.text.tidy_block` |
| Assemble elements and assign their offsets | `extraction.elements.ElementList` |
| Track the heading path | `extraction.readers.base.SectionStack` |
| Decide a table's header row and render rows | `extraction.tables.TableReader` |
| Import an optional dependency with a clear error | `extraction.readers.base.require` |

`ElementList.build()` is not optional. It joins the element texts into the document text and
gives every element its `char_start` / `char_end` in it, which is what makes a retrieved chunk
traceable back to the source. Do not assign offsets by hand.

## Rules a reader must follow

1. **Never assume the encoding.** `decode_bytes` tries UTF-8, then UTF-16 by BOM, then cp1252,
   then latin-1, and reports which worked.
2. **Normalise newlines before any line-based parsing.** A file written on Windows has CRLF
   endings; a pattern written against `\n` silently fails on it. (This was a real bug: Markdown
   front matter was being parsed as a heading on CRLF files.)
3. **Strip unstorable characters.** NUL is rejected by Postgres `text` columns.
4. **Raise `ReaderError` for a file that is not what its extension claims** — the extractor logs
   it and moves on, so one bad file never stops a run.
5. **Use `require()` for an optional dependency** so the error names the package to install
   instead of an ImportError at start-up.
6. **Remove boilerplate**, not content: navigation and cookie banners in HTML, quoted replies in
   email. Decide this from structure (the container holding the prose) rather than from a
   site-specific selector.
7. **Put nothing format-specific into chunking.** If a chunking rule seems to need to know the
   format, the element model is missing a field or a kind — extend it, in `extraction/base.py`.
8. **Log and continue on partial failure.** A sheet that cannot be read should not lose the
   workbook.

## Testing a reader

`tests/test_readers.py` writes a small real file per format and checks the same four things:

```python
def test_my_format_reads_every_block_kind(write: Write) -> None:
    result = MyReader().read(write("a.rtf", SAMPLE))
    check_offsets(result)  # every element is at its own offsets
    assert kinds(result) == ["heading", "paragraph", ...]  # the right kinds, in order
    assert of_kind(result, ElementKind.LIST_ITEM)[0].lead_in == "Do the following:"
    assert result.metadata["reader"] == "rtf"  # via the extractor
```

Cover the cases that break real files: an empty file, one huge paragraph, a non-UTF-8 encoding,
CRLF endings, Unicode text, and a file that is not the format it claims to be. Do not call any
network service; a reader has no reason to.

## Formats covered today

`pdf`, `markdown`, `html`, `docx`, `csv` (CSV/TSV/PSV), `excel` (xlsx/xlsm), `structured`
(JSON/JSONL/YAML), `email` (eml/mbox), `code` (about 40 languages), and `text` as the fallback
for every other extension — so an unknown format is still ingested, as plain text with light
structure detection, rather than skipped.

Not covered: slide decks (`.pptx` — `page` already carries a slide number, so a reader is all
that is missing), `.doc` and `.xls` (the old binary formats), and RTF.
