"""Text embeddings behind a port.

The default ``HashEmbedder`` is a deterministic feature-hashing bag-of-words vector. It gives
lexical similarity with no external service, which is enough for retrieval to work end to end and
to be tested. A semantic provider is swapped in by registering another ``Embedder``.
"""

import hashlib
import math
import re
from typing import Protocol

from memory.models import EMBEDDING_DIMENSIONS

_TOKEN = re.compile(r"[a-z0-9]+")
_STOPWORDS = frozenset(
    [
        "a",
        "an",
        "and",
        "are",
        "as",
        "at",
        "be",
        "by",
        "for",
        "from",
        "has",
        "have",
        "in",
        "is",
        "it",
        "its",
        "of",
        "on",
        "or",
        "that",
        "the",
        "this",
        "to",
        "was",
        "were",
        "with",
    ]
)


def _stem(token: str) -> str:
    """Cheap plural folding so "headline" and "headlines" share a bucket."""
    if len(token) > 4 and token.endswith("ies"):  # noqa: PLR2004
        return token[:-3] + "y"
    if len(token) > 3 and token.endswith("s") and not token.endswith("ss"):  # noqa: PLR2004
        return token[:-1]
    return token


class Embedder(Protocol):
    def embed(self, texts: list[str]) -> list[list[float]]: ...


class HashEmbedder:
    def embed(self, texts: list[str]) -> list[list[float]]:
        return [self._vector(text) for text in texts]

    @staticmethod
    def _vector(text: str) -> list[float]:
        vector = [0.0] * EMBEDDING_DIMENSIONS
        tokens = [_stem(t) for t in _TOKEN.findall(text.lower()) if t not in _STOPWORDS]
        for token in tokens:
            digest = hashlib.blake2b(token.encode(), digest_size=8).digest()
            bucket = int.from_bytes(digest[:4], "big") % EMBEDDING_DIMENSIONS
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[bucket] += sign
        norm = math.sqrt(sum(v * v for v in vector)) or 1.0
        return [v / norm for v in vector]


_embedder: Embedder = HashEmbedder()


def get_embedder() -> Embedder:
    return _embedder


def set_embedder(embedder: Embedder) -> None:
    global _embedder  # noqa: PLW0603
    _embedder = embedder
