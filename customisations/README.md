# Customisations — where one client's code lives

Every client runs the same image. Almost everything a client needs differently is already
configuration: which documents, which chunking, which models, which limits, which rate limit.
What is left over — a file format only they have, a data source only they use — needs code.

Code is where per-client work turns into per-client forks, so client code does not go in the
pipeline. It goes in a module here, reached only through the Protocols the pipeline already
defines, and named in one config value:

```yaml
customisation: acme        # loads customisations/acme
```

**The failure this exists to prevent:** five copies of the pipeline in which one bug has to be
found and fixed five times, differently. A reader in `customisations/acme/` is a reader the
shared chunker, retriever and API know nothing about; a fix to any of those still reaches ACME.

## The shape of a client module

```
customisations/
  acme/
    __init__.py       exposes `customisation = Customisation(...)`
    readers.py        their formats
    extractor.py      their data source, if the shared code cannot enumerate it
```

```python
# customisations/acme/__init__.py
from customisations import Customisation
from customisations.acme.readers import LedgerReader

customisation = Customisation(readers=(LedgerReader(),))
```

That is the whole contract. `customisations.load()` imports the module and expects exactly that
object; anything else — a missing module, a missing `customisation`, one of the wrong type — stops
start-up. It does **not** fall back to "no customisation": a client's reader silently not loading
would mean their documents were quietly ingested by the plain-text fallback, which looks like
working software and produces wrong answers.

## What a module may provide

### `readers` — a format only this client has

Extra `Reader`s, registered **ahead of** the built-in ones, so a client can also claim an
extension the shared code handles — a client whose `.md` files are not Markdown needs to be able
to say so.

Write it exactly as `context/adding-a-reader.md` describes: describe the file as
`DocumentElement`s and make no chunking decisions. Its elements then go through the same chunking
rules as every other format, which is why a reader is never a fork of the chunker. Bump its
`version` when you change what it extracts, or already-ingested documents keep their old chunks.

### `extractors` — a data source the shared code cannot enumerate

Extra `Extractor`s, keyed by the `extraction.provider` name that selects them:

```python
customisation = Customisation(extractors={"ledger_api": LedgerApiExtractor})
```

```yaml
extraction:
  provider: ledger_api
```

See `context/adding-an-extractor.md`. The contract still holds for client code: enumerate
candidates cheaply, parse only in `read()`, and raise `ExtractionError` for a source you cannot
read so the run reports it and carries on.

An unknown provider name is rejected when the extractor is built — the start of a run — with a
message naming the ones that exist. It cannot be a config-load error, because which providers
exist depends on this module, and `settings.py` deliberately does not import it.

## What a module must not do

- **Not a fork of a pipeline stage.** Chunking, retrieval, generation and the API are shared. A
  client-specific chunking rule is a missing field or kind on the element model — extend
  `extraction/base.py`, where every client benefits.
- **Not a second source of configuration.** Sizes, models, thresholds and limits belong in that
  client's `config.yaml`. A constant here is a value nobody can tune without a deploy.
- **Not a place for secrets.** Environment variables only, as everywhere else.
- **Not a substitute for asking whether it is general.** If two clients would want it, it belongs
  in the shared codebase. If none of the hooks fit, that is the signal to look again — usually the
  answer is config, or a field on an element, not a new seam.

## Testing one

`tests/test_customisations.py` writes client modules into a temporary package and exercises the
whole boundary — a reader loaded, winning a contested extension, reaching the extractor and
chunking normally; an extractor selected by provider name; and every way of getting the name
wrong stopping start-up. It doubles as the worked example, and a client module's own tests belong
beside it in the same shape.
