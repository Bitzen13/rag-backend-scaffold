"""Building an ingestion pipeline from settings.

One place, so `scripts/ingest.py` and the internal admin route cannot come to ingest differently
— the same failure that `services/factory.build_query_service` exists to prevent on the query
side.
"""

import customisations
from chunking.factory import build_chunkers
from embedding.factory import build_embedding_provider
from extraction.factory import build_extractor
from ingestion.pipeline import IngestionPipeline, SessionFactory
from settings import ExtractionConfig, Settings
from vector_store.pgvector_store import PgVectorStore


def build_ingestion_pipeline(
    settings: Settings,
    session_factory: SessionFactory,
    *,
    extraction: ExtractionConfig | None = None,
) -> IngestionPipeline:
    """The pipeline a run uses. `extraction` overrides `settings.extraction` (the CLI's --path).

    Raises before any work starts — for a customisation that does not resolve, an extraction
    provider that does not exist, or a missing provider secret — rather than part way through a
    run that has already spent money on embeddings.
    """
    client = customisations.load(settings.customisation)
    return IngestionPipeline(
        extractor=build_extractor(extraction or settings.extraction, client),
        chunkers=build_chunkers(settings),
        embedder=build_embedding_provider(settings),
        session_factory=session_factory,
        vector_store_factory=PgVectorStore,
    )
