"""Question answering: retrieve context, build the prompt, call the LLM."""

import logging
import uuid
from dataclasses import dataclass

from generation.base import LLMProvider
from generation.prompt import SYSTEM_PROMPT, PromptPassage, build_user_prompt
from retrieval.retriever import RetrievedChunk, Retriever
from vector_store.base import VectorFilter

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class SourceReference:
    chunk_id: uuid.UUID
    source_uri: str
    excerpt: str


@dataclass(frozen=True)
class Answer:
    answer: str
    sources: list[SourceReference]


def _excerpt(text: str, max_chars: int) -> str:
    text = " ".join(text.split())
    return text if len(text) <= max_chars else text[: max_chars - 1].rstrip() + "…"


class QueryService:
    def __init__(self, *, retriever: Retriever, llm: LLMProvider, excerpt_max_chars: int) -> None:
        self._retriever = retriever
        self._llm = llm
        self._excerpt_max_chars = excerpt_max_chars

    async def answer(self, question: str, filters: VectorFilter | None = None) -> Answer:
        chunks = await self._retriever.retrieve(question, filters)
        # The LLM is always called. With no chunk above the similarity threshold it gets an empty
        # context, so it can still handle greetings and small talk, and decline factual questions.
        user_prompt = build_user_prompt(
            question, [PromptPassage(c.source_uri, c.text) for c in chunks]
        )
        logger.debug(
            "sending %d chunks to the LLM\n--- system prompt ---\n%s", len(chunks), SYSTEM_PROMPT
        )
        logger.debug("--- user prompt ---\n%s", user_prompt)
        reply = await self._llm.generate(system=SYSTEM_PROMPT, user=user_prompt)
        logger.debug("--- LLM reply ---\n%s", reply)
        return Answer(reply, [self._reference(c) for c in chunks])

    def _reference(self, chunk: RetrievedChunk) -> SourceReference:
        return SourceReference(
            chunk_id=chunk.chunk_id,
            source_uri=chunk.source_uri,
            excerpt=_excerpt(chunk.text, self._excerpt_max_chars),
        )
