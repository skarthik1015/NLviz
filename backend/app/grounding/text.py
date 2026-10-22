"""Text normalization shared by corpus building and query-time retrieval.

Jammi's lexical analyzers do not fold accents (issue J10: "São" does not match "sao"), so both the
corpus and the query are folded here before lexical indexing and search.
"""
from __future__ import annotations

import unicodedata


def fold(text: str) -> str:
    """Lowercase, strip accents, and reduce punctuation/underscores to single spaces."""
    ascii_text = unicodedata.normalize("NFKD", str(text)).encode("ascii", "ignore").decode()
    return " ".join("".join(c if c.isalnum() else " " for c in ascii_text.lower()).split())


def trigrams(text: str) -> str:
    """Space-separated character trigrams of each folded word (with word boundaries), for typo
    tolerance through a plain token index: "paolo" still shares most trigrams with "paulo"."""
    grams: list[str] = []
    for word in fold(text).split():
        padded = f"#{word}#"
        if len(padded) <= 3:
            grams.append(padded)
        else:
            grams.extend(padded[i:i + 3] for i in range(len(padded) - 2))
    return " ".join(grams)
