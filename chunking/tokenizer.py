"""Token counting: chunk sizes are measured in the embedding model's own tokens.

Word counts were a stand-in. They mis-measure anything that is not English prose (a table row of
numbers, CJK text, code, long identifiers) by a factor of two or more, which is how chunks end up
silently truncated at the embedding model's input limit. Sizes here come from the real tokenizer,
with a word-based fallback that is only used when the tokenizer cannot be loaded.
"""

import logging
from collections.abc import Collection
from typing import Literal, Protocol

logger = logging.getLogger(__name__)

TokenizerKind = Literal["auto", "words"]

# Maximum tokens per input for embedding models we know. A model that is not listed is not
# guessed at: `chunking.max_chunk_tokens` must then be set explicitly in config.yaml.
EMBEDDING_INPUT_LIMITS: dict[str, int] = {
    "text-embedding-3-small": 8191,
    "text-embedding-3-large": 8191,
    "text-embedding-ada-002": 8191,
}
# The hard cap sits this far below the model's limit, so that an approximate size estimate during
# chunking can never push a finished chunk over the real limit.
INPUT_LIMIT_SAFETY_FRACTION = 0.95

_FALLBACK_ENCODING = "cl100k_base"


class Encoding(Protocol):
    """The part of a tiktoken encoding this module uses."""

    @property
    def name(self) -> str: ...

    def encode(self, text: str, *, disallowed_special: Collection[str] = ...) -> list[int]: ...


class Tokenizer(Protocol):
    @property
    def name(self) -> str:
        """Recorded with each ingested source, so a size is always interpretable later."""
        ...

    def count(self, text: str) -> int:
        """Number of tokens the embedding model will see for this text."""
        ...


class WordTokenizer:
    """Whitespace-delimited words. A rough approximation of tokens, used only as a fallback."""

    @property
    def name(self) -> str:
        return "words"

    def count(self, text: str) -> int:
        return len(text.split())


class TiktokenTokenizer:
    """Exact token counts from the OpenAI tokenizer used by the configured embedding model."""

    def __init__(self, encoding: Encoding) -> None:
        self._encoding = encoding

    @property
    def name(self) -> str:
        return f"tiktoken:{self._encoding.name}"

    def count(self, text: str) -> int:
        # disallowed_special=() matters: documents legitimately contain strings such as
        # "<|endoftext|>", and the default would raise instead of counting them.
        return len(self._encoding.encode(text, disallowed_special=()))


def embedding_input_limit(embedding_model: str) -> int | None:
    """The model's maximum tokens per input, or None when it is not known here."""
    return EMBEDDING_INPUT_LIMITS.get(embedding_model)


def default_max_chunk_tokens(embedding_model: str) -> int | None:
    """The hard cap to use when config does not set one, or None for an unknown model."""
    limit = embedding_input_limit(embedding_model)
    return int(limit * INPUT_LIMIT_SAFETY_FRACTION) if limit is not None else None


def build_tokenizer(kind: TokenizerKind, embedding_model: str) -> Tokenizer:
    """Resolve `chunking.tokenizer`.

    "auto" loads the tokenizer of the configured embedding model and falls back to word counting
    with a warning if tiktoken is unavailable (chunks then come out somewhat smaller than asked
    for, but never larger, because the hard cap is still enforced on exact counts).
    """
    if kind == "words":
        return WordTokenizer()
    try:
        import tiktoken
    except ImportError:
        logger.warning(
            "tiktoken is not installed: falling back to word counts for chunk sizes "
            "(pip install tiktoken, or set chunking.tokenizer: words to silence this)"
        )
        return WordTokenizer()
    try:
        encoding = tiktoken.encoding_for_model(embedding_model)
    except KeyError:
        encoding = tiktoken.get_encoding(_FALLBACK_ENCODING)
        logger.info(
            "no tokenizer registered for embedding model %r; using %s",
            embedding_model,
            _FALLBACK_ENCODING,
        )
    except Exception as exc:
        logger.warning("could not load a tokenizer (%s): falling back to word counts", exc)
        return WordTokenizer()
    return TiktokenTokenizer(encoding)
