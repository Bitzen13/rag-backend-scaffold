# Chunking Guide — which strategy and options for which content

Chunking decides what a retrieved passage looks like, and so decides what the LLM can answer.
This is the decision guide; `adding-a-reader.md` covers supporting a new file format.

Every document must be listed under `chunking.documents` in `config.yaml` with its strategy. A
document with no entry is not ingested, and ingestion says so by name — the choice is deliberate
rather than guessed.

## The two strategies

| | `document_aware` | `fixed_size` |
|---|---|---|
| Works on | the elements a reader found (headings, paragraphs, table rows, list items, code, Q&A, turns, messages) | the document text as one stream |
| Chunk boundaries | element boundaries | word boundaries inside a token budget |
| Each chunk carries | its heading path, table caption, list lead-in, question or speakers | nothing but the text |
| Use it for | anything a reader can describe: PDF, Markdown, HTML, DOCX, CSV, Excel, JSON, YAML, email, source code, structured plain text | text with no structure worth preserving, or a format whose reader found none |

**Default to `document_aware`.** It falls back to fixed-size windows by itself when a document
turns out to have no elements, so choosing it costs nothing on an unstructured file. Choose
`fixed_size` deliberately, for a file that is genuinely a wall of text.

## What `document_aware` does per element kind

| Element | Rule | Why |
|---|---|---|
| Table row | one chunk, prefixed with the table caption and section, cells rendered `Header: value \| ...` | a row is the unit someone asks about; without its column names it is a list of numbers |
| Code block | stays whole; split only if it alone exceeds the budget | half a function answers nothing |
| List items | grouped under the line that introduces them, up to the budget | an item ("Run the migrations") is meaningless without its lead-in ("To deploy:") |
| Question + answer | the question becomes the chunk's context, so every part of a long answer repeats it | a question is how someone will search for the answer |
| Transcript turns | grouped into a window of the conversation (2 minutes), each turn labelled with its speaker | an exchange reads as a unit; a single turn usually does not |
| Message (email) | one chunk, with From/To/Date/Subject in the text | who sent what, and when, is part of the answer |
| Paragraphs | packed together while they share a heading path and fit the budget | keeps a section's prose together, and never mixes two sections |

A chunk's context line is **counted against** `chunk_size`, not added on top of it, so a chunk is
never larger than it was configured to be. A heading path too deep to fit its quarter-share of
the budget is trimmed from the outside in: the nearest heading is the informative one.

## Sizes

Sizes are in **tokens of the configured embedding model**, counted with that model's own
tokenizer (`chunking.tokenizer: auto`). Word counts were the old approximation; they under-count
tables, code, CJK and long identifiers by a factor of two or more, which is how chunks end up
silently truncated.

- `chunk_size` — the target. 300–600 suits prose. Smaller (150–300) sharpens retrieval on
  reference material where the answer is one fact; larger (600–1000) suits discursive text where
  the answer needs surrounding argument.
- `chunk_overlap` — how much of the previous chunk a new one repeats. 10% of `chunk_size` is a
  reasonable start. In `prose_splitting: sentence` the overlap is taken in **whole sentences**, so
  an overlap smaller than a typical sentence produces no overlap at all; allow 40–60 tokens.
- `max_chunk_tokens` — the hard cap. Derived from the embedding model's input limit when unset.
  No chunk ever exceeds it, so nothing is ever silently truncated by the provider. An embedding
  model whose limit is not known to `chunking/tokenizer.py` must have this set explicitly, and
  config loading fails with that message rather than guessing.

## `prose_splitting`

Where `document_aware` cuts a paragraph that is too long for one chunk.

- `paragraph` (default) — cuts between words. Cheapest, works on every script.
- `sentence` — cuts between sentences, and overlaps by whole sentences, so a chunk never starts
  or ends mid-thought. Costs nothing extra at ingestion time. Sentence detection is
  script-agnostic (Latin, CJK, Devanagari, Arabic, Urdu, Ethiopic) and falls back to word windows
  for text with no sentence punctuation at all, such as Thai.

Prefer `sentence` for prose. `paragraph` is the better choice when the "prose" is not sentences —
log lines, reference lists, transcripts of speech without punctuation.

`prose_splitting` is a setting of `document_aware`; config rejects it on `fixed_size` rather than
silently ignoring it.

## Per-document options

A document may override the global sizes and splitting mode:

```yaml
chunking:
  chunk_size: 500
  chunk_overlap: 50
  prose_splitting: sentence
  documents:
    notes.md: fixed_size                  # the short form: strategy only
    reference-tables.pdf:
      strategy: document_aware
      chunk_size: 250                     # a fact per chunk
      chunk_overlap: 0                    # rows do not need overlap
    long-essay.pdf:
      strategy: document_aware
      chunk_size: 800
      prose_splitting: sentence
```

Every effective combination is validated at load time: `chunk_overlap` below `chunk_size`,
`chunk_size` within the hard cap.

## Measuring instead of guessing

Two tools, both of which should be run **before** changing any of the above, so that "better" is
a measurement rather than an impression.

```bash
# Chunk quality: sizes, oversized or empty chunks, duplicates, suspicious cuts.
# Needs no database and no API key.
python scripts/ingest.py --dry-run --qa-json evaluation/baselines/chunk-qa.json

# Retrieval quality: recall@k and MRR over evaluation/questions.yaml, through the real
# retrieval path. Needs the corpus ingested and an embedding API key.
python scripts/evaluate.py --save-baseline evaluation/baselines/retrieval.json

# After a change:
python scripts/evaluate.py --baseline evaluation/baselines/retrieval.json   # non-zero on a regression
```

`--dry-run` also writes every chunk to `chunked/<source_uri>.chunks.txt`, which is the fastest
way to see what a setting actually did.

**No baselines ship with this template**, and that is deliberate: a baseline recorded on
somebody else's documents would make the regression gate pass or fail for reasons that have
nothing to do with your corpus, which is worse than having no gate. Record both once your real
documents are ingested and `evaluation/questions.yaml` holds your own questions, then commit
them — `evaluation/baselines/README.md` has the commands.

### Reading the quality report

- **oversized / empty** are defects: a chunk larger than `chunk_size`, or one with no source text
  of its own. These should be zero.
- **undersized** is informational. A short section legitimately becomes a short chunk. Table
  rows, code blocks, lists and messages are never counted as undersized.
- **mid_sentence_start / mid_word_end** usually points at the *reader*, not the chunker: the
  element itself began mid-sentence because the source split a paragraph around a figure. Check
  the chunk file before changing chunking settings.
- **duplicate** is worth a look: repeated boilerplate crowds out other results. Consider a
  `extraction.pdf.strip_patterns` entry or a `skip_sections` entry.
- **mostly_url** flags a chunk that is mostly a link, typical of a references section. Those are
  usually best removed with `extraction.pdf.skip_sections`.

## What is deliberately not implemented

Three further ideas, each gated on the harness showing a gain — `design-decisions.md` records them
under *Considered and not adopted*. None has been measured, so none is implemented: a retrieval
feature with a cost and no measured benefit is worse than its absence:

- **`semantic` prose splitting** (embed sentences, cut where neighbouring similarity drops). It
  needs an embedding call per sentence at ingestion time. `chunking/prose.py` defines a
  `ProseSplitter` protocol, so it drops in as a third splitter when someone has a measurement to
  justify it.
- **`parent_child`** (retrieve small chunks, pass the surrounding section to the LLM). Needs a
  parent reference stored per chunk.
- **`contextual`** (an LLM writes a one-line note on where each chunk sits, prepended before
  embedding). Costs an LLM call per chunk.

`propositions` (rewriting text into atomic facts with an LLM) is not planned at all, on cost.
