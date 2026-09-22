# Baselines

A baseline is a recorded measurement of *your* corpus, and it is the reference every later change
to chunking or retrieval is compared against. This directory ships empty on purpose: a baseline
taken from someone else's documents is worse than none, because the regression gate would then
pass or fail for reasons that have nothing to do with your project.

Record both once your real corpus is ingested and your `../questions.yaml` is written:

```bash
# Chunk quality: sizes, oversized or empty chunks, duplicates, suspicious cuts.
# Needs no database and no API key.
python scripts/ingest.py --dry-run --qa-json evaluation/baselines/chunk-qa.json

# Retrieval quality: recall@k and MRR through the real retrieval path.
# Needs the corpus ingested and an embedding API key.
python scripts/evaluate.py --save-baseline evaluation/baselines/retrieval.json
```

Commit both files. From then on:

```bash
python scripts/evaluate.py --baseline evaluation/baselines/retrieval.json  # non-zero on a regression
```

Re-record a baseline only when a change improved the numbers and you mean to keep it — not to make
a failing check pass. `../../context/chunking-guide.md` explains how to read the chunk quality
report, and which flags are defects rather than properties of the source documents.
