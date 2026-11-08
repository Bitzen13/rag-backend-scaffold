"""Building an ingestion pipeline from settings.

One place, so `scripts/ingest.py` and the internal admin route cannot come to ingest differently
— the same failure that `services/factory.build_query_service` exists to prevent on the query
side.
"""

from app.chunking.factory import build_chunkers
from app.embedding.factory import build_embedding_provider
from app.extraction.factory import build_extractor
from app.ingestion.pipeline import IngestionLimits, IngestionPipeline, SessionFactory
from app.settings import ExtractionConfig, Settings
from app.vector_store.pgvector_store import PgVectorStore


def build_ingestion_pipeline(
    settings: Settings,
    session_factory: SessionFactory,
    *,
    extraction: ExtractionConfig | None = None,
) -> IngestionPipeline:
    """The pipeline a run uses. `extraction` overrides `settings.extraction` (the CLI's --path).

    Raises before any work starts — for a missing provider secret — rather than part way through a
    run that has already spent money on embeddings.
    """
    config = extraction or settings.extraction
    limits = config.limits
    return IngestionPipeline(
        extractor=build_extractor(config),
        chunkers=build_chunkers(settings),
        embedder=build_embedding_provider(settings),
        session_factory=session_factory,
        vector_store_factory=PgVectorStore,
        limits=IngestionLimits(
            max_extracted_chars=limits.max_extracted_chars,
            max_chunks_per_document=limits.max_chunks_per_document,
            max_metadata_bytes=limits.max_metadata_bytes,
            max_delete_fraction=limits.max_delete_fraction,
        ),
    )
