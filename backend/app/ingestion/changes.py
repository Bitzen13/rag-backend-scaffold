"""Deciding what a run has to do with each source, before any of it is opened.

Extraction is by far the most expensive part of a run — on the sample corpus, re-parsing nine
files to discover that nothing changed costs about 49 seconds, where hashing their bytes costs
50 milliseconds. So every question that can be answered without opening a file is answered here,
against the raw hash and the plan recorded with each source at its last ingestion.
"""

from app.chunking.base import ChunkPlan
from app.db.models import JsonObject, SourceStatus
from app.db.repositories.sources import SourceState
from app.extraction.base import SourceCandidate

# Keys of the recorded plan that identify the reader rather than the chunking settings.
READER_KEYS = ("reader", "reader_version")


def plan_record(plan: ChunkPlan, candidate: SourceCandidate) -> JsonObject:
    """What is stored in `sources.chunking`: the chunking plan, plus the reader behind the text.

    The reader belongs here because a reader *improvement* changes the extracted text without
    changing the file's bytes. Without its version recorded, a better reader would never reach
    documents that are already ingested.
    """
    return {
        **plan.as_metadata(),
        "reader": candidate.reader,
        "reader_version": candidate.reader_version,
    }


def describe_difference(stored: JsonObject, record: JsonObject, keys: tuple[str, ...] = ()) -> str:
    """The keys whose values differ, as `key: old -> new`, for the run's log."""
    compared = keys or tuple(sorted(set(stored) | set(record)))
    return ", ".join(
        f"{key}: {stored.get(key)!r} -> {record.get(key)!r}"
        for key in compared
        if stored.get(key) != record.get(key)
    )


def reason_to_open(
    state: SourceState | None, candidate: SourceCandidate, record: JsonObject
) -> str | None:
    """Why this source must be opened, or None if it can be skipped without being parsed.

    Only the raw hash and the recorded plan are consulted, so deciding costs no extraction. A
    source that gets past every check here is byte-for-byte what was ingested, read by the same
    version of the same reader, and chunked the way config would chunk it now.
    """
    if state is None:
        return "new source"
    if state.status != SourceStatus.ACTIVE.value:
        return f"previously marked {state.status}"
    if not state.source_hash:
        return "no raw hash recorded for it yet"
    if state.source_hash != candidate.source_hash:
        return "the file changed"
    stored = state.chunking
    reader_change = describe_difference(stored, record, READER_KEYS)
    if reader_change:
        return f"the reader changed ({reader_change})"
    plan_change = describe_difference(stored, record)
    if plan_change:
        return f"the chunking settings changed ({plan_change})"
    return None
