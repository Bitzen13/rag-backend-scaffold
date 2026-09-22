# Adding an Extractor — supporting a new data source

An extractor's only job is to say **what sources exist right now**, cheaply, and to parse one when
asked. That is a different job from a reader's: a reader turns one file of one format into
elements (`adding-a-reader.md`), while an extractor decides what there is to read at all.

| You have | You need |
|---|---|
| files on disk in a format nothing handles yet | a **reader** — `adding-a-reader.md` |
| content somewhere other than the configured directory (a web crawl, Notion, S3, a database, a ticketing API) | an **extractor** — this document |

The rest of the pipeline is written against `SourceCandidate` and `ExtractedDocument`, so a new
data source changes nothing downstream: no chunker changes, no retrieval changes, no API changes.

## The contract

`extraction/base.py`:

```python
class Extractor(Protocol):
    @property
    def source_type(self) -> str: ...  # goes in sources.source_type for every candidate
    def candidates(self) -> Iterable[SourceCandidate]: ...
```

```python
@dataclass(frozen=True, kw_only=True)
class SourceCandidate:
    source_type: str
    source_uri: str  # with source_type, the unique key of the source
    source_hash: str  # of the raw content; "" forces a read
    reader: str = ""  # which reader will parse it, if that is known without parsing
    reader_version: int = 0  # bumping it re-extracts everything that reader handles

    def read(self) -> ExtractedDocument: ...  # parse, only when asked
```

## The two rules that matter

**1. `candidates()` must be cheap, and must parse nothing.**

A run decides what to open *before* opening anything: it enumerates candidates, loads the stored
state for the source type in one query, and opens only what is new, changed, previously deleted,
or recorded against different chunking settings. On the sample corpus that decision costs 0.031s
where parsing costs 69s — about two thousandfold on a run with nothing to do, and the whole reason
the interface is shaped this way. An extractor that parses during enumeration throws that away
silently: everything still works, and every run costs a full parse.

So `source_hash` has to be obtainable without parsing. For files it is a hash of the bytes. For an
API, use whatever the source already gives you that changes when the content does — an ETag, a
`version`, a `content_hash`, a `last_edited_time` **combined with** something content-derived.

If there is genuinely nothing, return `source_hash=""`. That forces a read every run, which is
honest: the alternative is a stale chunk, and a stale chunk is a wrong answer. Do not substitute a
timestamp you do not trust — mtime is rejected for exactly this reason, because cloud sync,
`cp -p` and rsync all preserve it while content changes.

**2. `read()` is the only place that parses, and it raises `ExtractionError` when it cannot.**

The run logs that source as `failed` and carries on with the others. One unreachable page must not
stop a corpus. Never return an empty `ExtractedDocument` instead: empty text is indistinguishable
from a document that really is empty, and it would replace good chunks with nothing.

## Deletion comes for free — if you enumerate completely

`candidates()` is also taken to be **exactly the set of sources that still exist**. Anything
stored, not already deleted, and missing from it is marked `deleted` and loses its chunks and
vectors. That is deletion handled with no extra code — and it is why a partial enumeration is
dangerous. An extractor that returns half the sources because an API paginated and the second page
failed will have the other half deleted.

So: if enumeration fails part way, **raise**, do not return what you have. A run that stops with an
error is recoverable; one that silently deletes a client's corpus is not.

## A minimal extractor

```python
SOURCE_TYPE = "notion"


@dataclass(frozen=True, kw_only=True)
class NotionCandidate(SourceCandidate):
    page_id: str
    client: NotionClient

    def read(self) -> ExtractedDocument:
        try:
            blocks = self.client.blocks(self.page_id)
        except NotionApiError as exc:
            raise ExtractionError(f"{self.source_uri}: {exc}") from exc
        elements = ElementList()
        for block in blocks:
            elements.add(DocumentElement(kind=kind_of(block), text=block.text, section=...))
        text, placed = elements.build()
        if not text.strip():
            raise ExtractionError(f"{self.source_uri}: no text content")
        return ExtractedDocument(
            SOURCE_TYPE, self.source_uri, text, {"page_id": self.page_id}, placed
        )


class NotionExtractor:
    def __init__(self, config: ExtractionConfig) -> None:
        self._client = NotionClient(os.environ["NOTION_TOKEN"])  # secrets from the environment

    @property
    def source_type(self) -> str:
        return SOURCE_TYPE

    def candidates(self) -> Iterator[NotionCandidate]:
        # One cheap listing call. It must return every page, or raise.
        for page in self._client.list_pages():
            yield NotionCandidate(
                source_type=SOURCE_TYPE,
                source_uri=page.slug,  # stable; not the title, which people rename
                source_hash=page.content_hash,  # whatever changes when the content does
                reader="notion",
                reader_version=1,
                page_id=page.id,
                client=self._client,
            )
```

Use `extraction.elements.ElementList` and let `build()` assign the offsets — never by hand. They
are what makes a retrieved chunk traceable to its source.

## Choosing `source_uri`

It is half the unique key of a source and it is what change detection looks up, so it must be
**stable for the life of the content**. A slug or an id, not a title: rename a page whose
`source_uri` is its title and the run sees one source deleted and another added, re-embedding the
whole thing. Keep it human-readable if you can — it is what a citation shows the end user.

## Registering it

Two homes, and which one is a real decision:

**A client's own source → their customisation module.** Nobody else has it, so it does not belong
in the shared codebase (`customisations/README.md`):

```python
customisation = Customisation(extractors={"notion": NotionExtractor})
```

```yaml
customisation: acme
extraction:
  provider: notion
```

**A source every client could use → the shared codebase.** Add it to the dispatch in
`extraction/factory.py` beside `filesystem`. If two clients would want it, it is not a
customisation.

Either way an unknown `extraction.provider` is rejected while the extractor is built — at the
start of a run — with a message naming the ones available.

## Testing one

Fake the remote, never call it. `tests/test_customisations.py` shows the shape. Cover:

- **enumeration parses nothing** — the crucial one. Assert the fake client's parse method was
  never called during `candidates()`.
- `source_hash` changes when the content does, and not when it does not.
- a source that cannot be read raises `ExtractionError` and the run continues.
- enumeration that fails part way raises rather than returning a partial list.
- `source_uri` survives a rename of whatever the source calls a title.
- the elements produced are the kinds you intended (`adding-a-reader.md` has the table).

## What does not change

Nothing downstream. Same chunking rules, same embedding, same storage, same retrieval, same API.
If supporting a new data source seems to require touching a chunker, the element model is missing
a field or a kind — extend `extraction/base.py`, where every source benefits.
