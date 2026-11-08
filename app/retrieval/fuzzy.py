"""Turn a question into the short phrases used for typo-tolerant keyword matching."""

import re

MAX_WINDOW_WORDS = 3  # longest phrase tried, e.g. a multi-word name like "Large Magellanic Cloud"
MIN_PHRASE_CHARS = 3  # ignore phrases too short to carry any spelling signal
MAX_PHRASES = 60  # bounds the work for very long questions

# Question filler that says nothing about what the user is looking for. A phrase may contain these
# in the middle ("Sun of Persei") but never start or end with one.
STOPWORDS = frozenset(
    [
        "a",
        "an",
        "the",
        "of",
        "in",
        "on",
        "at",
        "to",
        "for",
        "from",
        "by",
        "with",
        "about",
        "as",
        "into",
        "over",
        "under",
        "is",
        "are",
        "was",
        "were",
        "be",
        "been",
        "am",
        "do",
        "does",
        "did",
        "has",
        "have",
        "had",
        "what",
        "which",
        "who",
        "whom",
        "whose",
        "when",
        "where",
        "why",
        "how",
        "tell",
        "me",
        "my",
        "you",
        "your",
        "i",
        "it",
        "its",
        "this",
        "that",
        "these",
        "those",
        "there",
        "here",
        "and",
        "or",
        "but",
        "if",
        "than",
        "then",
        "so",
        "can",
        "could",
        "would",
        "should",
        "will",
        "may",
        "might",
        "any",
        "some",
        "give",
        "show",
        "list",
        "please",
        "find",
        "get",
    ]
)

_WORD = re.compile(r"\w+")


def question_phrases(question: str) -> list[str]:
    """Runs of 1..MAX_WINDOW_WORDS consecutive words that could name what the user wants.

    Windows that start or end with a stop-word are skipped. The database additionally ignores any
    phrase that matches too many chunks, so words common in the data drop out on their own.
    """
    words = _WORD.findall(question)
    phrases: dict[str, None] = {}
    for size in range(1, MAX_WINDOW_WORDS + 1):
        for start in range(len(words) - size + 1):
            window = words[start : start + size]
            if window[0].lower() in STOPWORDS or window[-1].lower() in STOPWORDS:
                continue
            phrase = " ".join(window)
            if len(phrase.replace(" ", "")) >= MIN_PHRASE_CHARS:
                phrases.setdefault(phrase.lower(), None)
    return list(phrases)[:MAX_PHRASES]
