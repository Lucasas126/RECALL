"""Fakes for vector index tests: a deterministic embeddings provider that counts what it embeds."""
from __future__ import annotations

import hashlib
import math
import re
import threading

_DIMENSIONS = 64


def fake_vector(text: str) -> list[float]:
    """A bag-of-words vector: texts sharing words point in similar directions."""
    vector = [0.0] * _DIMENSIONS
    for word in re.findall(r"\w+", text.lower()):
        digest = hashlib.md5(word.encode("utf-8")).digest()
        vector[digest[0] % _DIMENSIONS] += 1.0
    norm = math.sqrt(sum(value * value for value in vector)) or 1.0
    return [value / norm for value in vector]


class FakeEmbeddings:
    """Stands in for an LLM provider's `invoke_embeddings`; records every request."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []
        self._lock = threading.Lock()

    def invoke_embeddings(self, texts: list[str], model: str) -> list[list[float]]:
        with self._lock:
            self.calls.append((model, list(texts)))
        return [fake_vector(text) for text in texts]

    @property
    def texts(self) -> list[str]:
        with self._lock:
            return [text for _, batch in self.calls for text in batch]

    def reset(self) -> None:
        with self._lock:
            self.calls.clear()
