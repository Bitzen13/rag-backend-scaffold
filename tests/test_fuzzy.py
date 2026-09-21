import uuid
from collections.abc import Sequence

from sqlalchemy.ext.asyncio import AsyncSession

from db.models import SourceStatus
from db.repositories.chunks import ChunkRepository, NewChunk
from db.repositories.sources import SourceRepository
from retrieval.fuzzy import question_phrases

# --- question -> phrases ---------------------------------------------------------------------


def test_phrases_are_windows_of_up_to_three_words_lowercased() -> None:
    phrases = question_phrases("Radius of VY Canis Majoris")
    assert {"vy canis majoris", "canis majoris", "vy canis", "canis", "majoris"} <= set(phrases)
    assert "radius of vy" in phrases  # a stop-word inside a window is fine


def test_windows_never_start_or_end_with_a_stopword() -> None:
    phrases = question_phrases("Tell me a fun fact about VX Sagittarii")
    assert "vx sagittarii" in phrases
    assert all(p.split()[0] not in {"tell", "me", "a", "about"} for p in phrases)
    assert all(p.split()[-1] not in {"tell", "me", "a", "about"} for p in phrases)
    assert "about vx sagittarii" not in phrases


def test_short_and_duplicate_phrases_are_dropped() -> None:
    phrases = question_phrases("VX vx Sun Sun")
    assert "vx" not in phrases  # fewer than 3 letters on its own
    assert phrases.count("sun") == 1


def test_only_filler_gives_no_phrases() -> None:
    assert question_phrases("What is the?") == []
    assert question_phrases("") == []


# --- fuzzy search in Postgres ----------------------------------------------------------------


async def add_chunks(
    session: AsyncSession,
    uri: str,
    texts: Sequence[str],
    *,
    source_type: str = "file",
    status: SourceStatus = SourceStatus.ACTIVE,
) -> list[uuid.UUID]:
    source = await SourceRepository(session).create(
        source_type=source_type, source_uri=uri, content_hash="h"
    )
    if status is not SourceStatus.ACTIVE:
        await SourceRepository(session).set_status(source, status)
    ids = [uuid.uuid4() for _ in texts]
    await ChunkRepository(session).add_many(
        [
            NewChunk(cid, source.id, i, t, 3, "m")
            for i, (cid, t) in enumerate(zip(ids, texts, strict=True))
        ]
    )
    return ids


STARS = [
    "Star name: VX Sagittarii | Solar radius: 1,456 | Method: AD",
    "Star name: KW Sagittarii | Solar radius: 1,009 | Method: AD",
    "Star name: RW Cygni | Solar radius: 1,103 | Method: AD",
]


async def search(session: AsyncSession, phrases: list[str], **overrides: object) -> list[uuid.UUID]:
    options: dict[str, object] = {
        "min_similarity": 0.6,
        "max_matches_per_phrase": 50,
        "limit": 5,
    }
    options.update(overrides)
    hits = await ChunkRepository(session).fuzzy_search(phrases, **options)  # type: ignore[arg-type]
    return [h.chunk_id for h in hits]


async def test_exact_phrase_ranks_the_matching_chunk_first(session: AsyncSession) -> None:
    vx, kw, _ = await add_chunks(session, "stars.md", STARS)
    assert (await search(session, ["vx sagittarii", "sagittarii"]))[0] == vx
    assert set(await search(session, ["sagittarii"])) == {vx, kw}


async def test_misspelled_phrase_still_matches(session: AsyncSession) -> None:
    vx, *_ = await add_chunks(session, "stars.md", STARS)
    assert (await search(session, ["vx sagitarii"]))[0] == vx


async def test_more_matching_words_outrank_a_single_shared_word(session: AsyncSession) -> None:
    vx, kw, _ = await add_chunks(session, "stars.md", STARS)
    ranked = await search(session, ["vx", "sagittarii", "vx sagittarii"])
    assert ranked.index(vx) < ranked.index(kw)


async def test_unrelated_phrase_matches_nothing(session: AsyncSession) -> None:
    await add_chunks(session, "stars.md", STARS)
    assert await search(session, ["zebra crossing"]) == []


async def test_phrase_matching_too_many_chunks_is_ignored(session: AsyncSession) -> None:
    await add_chunks(session, "stars.md", STARS)
    assert await search(session, ["radius"], max_matches_per_phrase=2) == []
    assert len(await search(session, ["radius"], max_matches_per_phrase=3)) == 3


async def test_higher_min_similarity_is_stricter(session: AsyncSession) -> None:
    await add_chunks(session, "stars.md", STARS)
    assert await search(session, ["sagitarii"], min_similarity=0.6) != []
    assert await search(session, ["sagitarii"], min_similarity=0.95) == []


async def test_inactive_sources_and_other_source_types_are_excluded(session: AsyncSession) -> None:
    await add_chunks(session, "old.md", STARS[:1], status=SourceStatus.STALE)
    (web,) = await add_chunks(session, "https://x", STARS[:1], source_type="web")
    assert await search(session, ["vx sagittarii"]) == [web]
    assert await search(session, ["vx sagittarii"], source_type="file") == []


async def test_no_phrases_or_zero_limit_returns_nothing(session: AsyncSession) -> None:
    await add_chunks(session, "stars.md", STARS)
    assert await search(session, []) == []
    assert await search(session, ["sagittarii"], limit=0) == []
