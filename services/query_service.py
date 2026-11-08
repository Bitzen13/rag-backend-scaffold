"""Question answering: retrieve context, build the prompt, call the LLM, check the output."""

import logging
import uuid
from dataclasses import dataclass

from generation.base import LLMProvider
from generation.prompt import SYSTEM_PROMPT, PromptPassage, build_user_prompt
from retrieval.retriever import RetrievedChunk, Retriever
from safety.output import OutputSanitizer
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
    def __init__(
        self,
        *,
        retriever: Retriever,
        llm: LLMProvider,
        excerpt_max_chars: int,
        output: OutputSanitizer | None = None,
    ) -> None:
        self._retriever = retriever
        self._llm = llm
        self._excerpt_max_chars = excerpt_max_chars
        # Never optional in effect: left unset it is the strictest policy (no link allowed).
        self._output = output or OutputSanitizer()

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
        # The output checks hold whatever the model did: an injected instruction it obeyed can
        # change the words, but cannot make a client fetch a URL (safety/output.py).
        answer = self._output.clean_text(reply, field="answer")
        return Answer(answer, [self._reference(c) for c in chunks])

    def _reference(self, chunk: RetrievedChunk) -> SourceReference:
        # Excerpts and source names reach the client without passing through the model, so they
        # are checked too. The excerpt is cleaned before it is cut, so truncation cannot leave
        # half a URL behind that the check would no longer recognise.
        text = self._output.clean_text(chunk.text, field="excerpt")
        return SourceReference(
            chunk_id=chunk.chunk_id,
            source_uri=self._output.clean_text(chunk.source_uri, field="source_uri"),
            excerpt=_excerpt(text, self._excerpt_max_chars),
        )
