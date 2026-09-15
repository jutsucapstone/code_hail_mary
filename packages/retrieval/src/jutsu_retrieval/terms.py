"""Words, for the searches that match text rather than vectors.

Claims (ADR 0028) and folders (ADR 0029) are found by what a question says as well as by
what it asks about. This module is the one place text becomes those words — a question's and
a folder path's — so a search and what it searches cannot disagree about them.

**Letters and digits only.** Nothing a person types can become query syntax — no `&`, `|`,
`!`, `:*`, `<->` or parenthesis survives — and the words are bound as parameters, never
interpolated into a statement.
"""

from __future__ import annotations

import re
from typing import Final

__all__ = [
    "MAX_FOLDER_WORDS",
    "MAX_TERMS",
    "folder_words",
    "query_terms",
    "tsquery_any",
    "words_of",
]

#: At most this many of a question's words reach a search.
MAX_TERMS: Final = 12

#: At most this many of a folder path's words are recorded for one document.
MAX_FOLDER_WORDS: Final = 32

#: The longest folder word recorded (`document_folder_words.word` is varchar(64)).
_MAX_WORD: Final = 64

_WORD: Final = re.compile(r"[a-z0-9]+")

#: A question's framing, which says nothing about what answers it. PostgreSQL's English
#: configuration drops its own stop words from a tsquery as well; `simple` drops none.
_FRAMING: Final = frozenset(
    {"what", "which", "who", "whom", "when", "where", "why", "how", "did", "does", "was",
     "were", "are", "the", "and", "for", "about", "with", "that", "this", "have", "has",
     "had", "should", "could", "would", "tell", "show", "list", "give", "any", "all",
     "their", "they", "them", "his", "her", "its", "our", "your", "you", "most", "important"}
)  # fmt: skip


def words_of(text: str) -> list[str]:
    """Every lower-case run of letters and digits in `text`, in order."""
    return _WORD.findall(text.lower())


def query_terms(question: str, *, ignore: frozenset[str] = frozenset()) -> list[str]:
    """The question's words, in order, without framing, duplicates or one- and two-letter
    words, and at most `MAX_TERMS` of them. `ignore` adds a search's own framing."""
    terms: list[str] = []
    for word in words_of(question):
        if len(word) < 3 or word in _FRAMING or word in ignore or word in terms:
            continue
        terms.append(word)
        if len(terms) >= MAX_TERMS:
            break
    return terms


def folder_words(path: str | None) -> list[str]:
    """A folder path's words, read the way a question's are.

    Letters and digits, three characters or more, each once, in order: `Projects/Astro_Agent`
    is the three words `projects`, `astro` and `agent` a person types. No framing is removed —
    a folder may be called anything — and at most `MAX_FOLDER_WORDS` are kept. A word longer
    than 64 characters is skipped; no question names a folder by one.
    """
    if not path:
        return []
    words: list[str] = []
    for word in words_of(path):
        if len(word) < 3 or len(word) > _MAX_WORD or word in words:
            continue
        words.append(word)
        if len(words) >= MAX_FOLDER_WORDS:
            break
    return words


def tsquery_any(terms: list[str]) -> str:
    """`a | b | c`: any of the words. The caller binds it; it is never formatted into SQL."""
    return " | ".join(terms)
