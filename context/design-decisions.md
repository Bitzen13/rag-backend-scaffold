# Design Decisions

The decisions that still bind new work, and the reasoning behind them. `architecture-context.md`
says what the system *is*; this file says why it is that and not something else, so that a change
which looks like an improvement can be checked against the reason the current shape was chosen.

Two kinds of entry: decisions that hold, and things considered and deliberately not adopted. The
second kind matters more — an idea rejected without a written reason gets re-proposed every few
months, and an idea rejected for a reason that has since stopped being true should be revisited.

---

## Decisions that hold

**One deployment per client, not one deployment with a tenant column.** Every table holds one
corpus, and an API key identifies a calling application within one deployment. Isolation is
physical: there is no `WHERE client_id = ...` that somebody has to remember to write, and no
migration that can leak one client's chunks into another's answers. The cost is real — N stacks to
upgrade, N databases to back up — and it is the cost that would justify revisiting this. See
*Multi-tenancy in one deployment*, below.

**Two hashes, because they answer different questions.** `sources.source_hash` is of the raw bytes
and gates "must I open this file at all"; `sources.content_hash` is of the extracted text and gates
"did opening it produce something different, so it needs re-embedding". Neither replaces the other.

**Change detection decides before extraction, not after it.** `Extractor.candidates()` enumerates
what exists without parsing any of it. On the template's original sample corpus, enumerating and
hashing 40MB of PDFs took 0.031s where parsing them took 69s. An extractor that parses during
enumeration throws that away silently: everything still works, and every run costs a full parse.
This is why `adding-an-extractor.md` leads with it.

**Shared state that must be right across workers lives in Postgres, in one atomic statement.** The
rate-limit count and the "one ingestion run at a time" lock are both in the database, not in
process memory. Production runs several Uvicorn workers; an in-memory counter gives each of them
the whole allowance, which makes the effective limit the configured one times the worker count.
Read-then-write fails the same way a size smaller: two workers both read 59 and both allow the 60th.

**A fast hash for API keys, not a slow KDF.** A key is 32 random bytes, so there is no low-entropy
secret to brute-force; SHA-256 plus `hmac.compare_digest` is the right trade. A KDF would be paid
on every request to defend against an attack the key's entropy already prevents.

**Redaction in the log formatter, not at the call site.** Every record passes through a formatter,
including records from libraries this project does not own and including exception tracebacks. A
filter or a careful caller can be forgotten; a formatter cannot be bypassed.

**CORS off by default, and the middleware not installed when it is empty.** A consumer frontend
calls this API from its own server, where CORS does not apply and where a key can be held safely. A
browser calling directly would have to hold that key where any visitor can read it. An entry in
`api.cors.allowed_origins` is a deliberate exception, not a default.

**Migrations are their own deploy step.** Not the image's `CMD`, not worker start-up: every worker
would race the others, and with more than one replica the schema would be applied by whichever
container booted first.

**Database statements are not retried inside a request.** A blind retry of something that may have
been a write is worse than the failure it hides. `pool_pre_ping` covers the one case worth retrying
transparently — a connection the server has already closed.

**A setting that cannot take effect is a load-time error.** Provider lists name only what is
implemented, `prose_splitting` is rejected on `fixed_size`, and an unresolvable `customisation`
stops start-up rather than falling back to none. A client's reader silently not loading means their
documents are ingested by the plain-text fallback, which looks like working software and returns
wrong answers.

**The model gets no tools and no network access.** It receives text and returns text, so the
worst a prompt injection hidden in a document can do is change an answer. It cannot take an
action or send data anywhere. No defence that relies on detecting injections is that strong,
because an adaptive attacker can get past detection. This is also why the output checks in
`safety-plan.md` concentrate on what an answer can make a client fetch. Adding a tool, an action,
or retrieval driven by the model changes the threat model, so `safety-plan.md` must be revised
first.

---

## Considered and not adopted

### Multi-tenancy in one deployment

Serving several client companies from one stack, with a `client_id` on every table and a filter on
every query.

**Not adopted.** Isolation would become a property of every query anyone writes, forever, and the
failure mode is silent: a missing filter returns another client's documents as a grounded answer
with citations. One deployment per client makes isolation physical, and the per-client cost is a
directory and a Compose project (`deploy/`).

**What would justify revisiting it:** enough clients that N stacks is the dominant operational
cost, *and* a corpus small enough per client that a database each is plainly wasteful. If it is
revisited, the finding that matters is that the API key already identifies a caller — the join from
key to tenant exists; what does not exist is an enforcement point that cannot be forgotten, and
that is what would have to be built first (row-level security, not a convention).

### A local embedding model

Running `sentence-transformers` in-process instead of calling OpenAI.

**Not adopted,** on cost in the other direction: it trades an API bill for a GPU or slow CPU
inference in the request path, plus a model to ship in the image and a dimension change that
requires a migration rebuilding `chunk_vectors`. `embedding/base.py` is the interface, so the work
is one implementation plus a name in the config choice.

**What would justify it:** a client whose data may not leave their infrastructure. That is a
requirement no amount of cost tuning answers, and it is the likeliest reason this gets built.

### Hosted vector databases (Pinecone, Qdrant, Weaviate)

**Not adopted.** pgvector keeps the vectors in the database that already holds the chunks, which is
what lets a re-sync write rows and vectors in one transaction. A separate vector service makes that
two systems that can disagree, and adds a second thing to run per client for a corpus size that
does not need it.

**What would justify it:** a corpus large enough that pgvector's HNSW index stops performing, which
is a measurement, not a feeling. `vector_store/base.py` is the interface; writing rows and vectors
in one transaction is the guarantee a new implementation would have to answer for.

### `semantic` prose splitting, `parent_child`, `contextual` chunking

**Not adopted, and gated on measurement.** Each has a real cost — an embedding call per sentence at
ingestion, a stored parent reference per chunk, an LLM call per chunk respectively — and none has
been measured against the retrieval harness. A retrieval feature with a cost and no measured
benefit is worse than its absence. `chunking/prose.py` defines a `ProseSplitter` protocol so a
semantic splitter drops in when someone has a number to justify it. `propositions` (rewriting text
into atomic facts with an LLM) is not planned at all, on cost. `chunking-guide.md` has the detail.

### A sliding-window rate limit

**Not adopted.** The fixed window costs a boundary burst — a caller can spend its allowance at the
end of one window and again at the start of the next. A sliding window costs a stored row per
request. For protecting an LLM bill from a runaway consumer, the burst is affordable and the row
per request is not.

### mtime or file size as the cheap change signal

**Not adopted.** Hashing the whole corpus already costs milliseconds, so a `stat()` fast path saves
nothing worth having — and mtime lies: cloud sync, `cp -p` and rsync without checksums all preserve
it while the content changes. A silently stale chunk is a wrong answer, which is worse than a
wasted parse.

### A fork per client

**Not adopted, and it is the one answer that is always wrong.** It is how one bug comes to be found
and fixed five times, differently. `customisations/README.md` and `architecture-context.md` §8 have
the seams to reach for instead, in order.
