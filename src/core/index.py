from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import chromadb
import chromadb.utils.embedding_functions as ef

from src.config.settings import (
    DATA_DIR,
    INDEX_CHUNK_WORDS,
    INDEX_COLLECTION,
    INDEX_DIR_NAME,
    INDEX_FILE_TYPES,
)
from src.core.sources.base import Source
from src.core.sources.registry import SourceRegistry
from src.providers.provider import LLMProvider
from src.utils.logger import get_logger

logger = get_logger("vector-index")


def _indexable_content(content: Any) -> str:
    """Convert persisted turn content to text without indexing attachment payloads."""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return str(content) if content is not None else ""
    parts: list[str] = []
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text":
            text = part.get("text")
            if isinstance(text, str):
                parts.append(text)
        elif part.get("type") in {"image_url", "input_audio", "video_url"}:
            parts.append(f"[{part['type']}]")
    return " ".join(parts)


class _ProviderEmbeddingFunction(ef.EmbeddingFunction):
    """Adapts a provider's batched invoke_embeddings so ChromaDB can call it."""

    def __init__(self, provider: LLMProvider, model: str):
        self._provider = provider
        self._model = model

    def name(self) -> str:  # required by ChromaDB
        return f"provider-{self._model}"

    def __call__(self, input: list[str]) -> list[list[float]]:
        return self._provider.invoke_embeddings(list(input), self._model)


class VectorIndex:
    """Per-agent semantic index over the registry's indexed sources.

    Chunks live in one Chroma collection, tagged with their originating
    ``source`` and full ``address`` so a hit can be traced back and re-read.
    Kept in sync incrementally via a per-file MD5 stored in chunk metadata.
    """

    def __init__(self, path: Path, registry: SourceRegistry, provider: LLMProvider, model: str):
        self._registry = registry
        self._model = model
        self._embedding_fn = _ProviderEmbeddingFunction(provider, model)
        self._client = chromadb.PersistentClient(path=str(path))
        self._collection = self._client.get_or_create_collection(
            name=INDEX_COLLECTION,
            embedding_function=self._embedding_fn,
            metadata={"hnsw:space": "cosine"},  # similarity = 1 - cosine distance, ~[0, 1]
        )

    @classmethod
    def for_agent(
        cls, user_id: str, agent_id: str, registry: SourceRegistry, provider: LLMProvider, model: str
    ) -> "VectorIndex":
        path = DATA_DIR / user_id / agent_id / INDEX_DIR_NAME
        path.mkdir(parents=True, exist_ok=True)
        return cls(path, registry, provider, model)

    # ------------------------------------------------------------------ sync

    def sync(self) -> None:
        """Reconcile every indexed source with the collection."""
        for source in self._registry.indexed_sources():
            self.sync_source(source.name)

    def sync_source(self, name: str) -> None:
        """Reconcile a single source: add new/changed files, drop deleted ones."""
        source = self._registry.get(name)
        if not source.indexed:
            return
        files = self._source_files(source)
        stored_md5, stored_ids = self._stored(source.name)

        for addr in set(stored_ids) - set(files):
            self._collection.delete(ids=stored_ids[addr])

        for addr, text in files.items():
            md5 = hashlib.md5(text.encode("utf-8")).hexdigest()
            if stored_md5.get(addr) == md5:
                continue
            if addr in stored_ids:
                self._collection.delete(ids=stored_ids[addr])
            self._index(source.name, addr, text, md5)

    # ------------------------------------------------------------------ query

    def query(self, query: str, top_k: int = 5) -> list[dict]:
        """Return the *top_k* most similar chunks as {address, document, score}."""
        count = self._collection.count()
        if not count:
            return []
        res = self._collection.query(
            query_texts=[query],
            n_results=min(top_k, count),
            include=["documents", "distances", "metadatas"],
        )
        out: list[dict] = []
        for doc, dist, meta in zip(res["documents"][0], res["distances"][0], res["metadatas"][0]):
            out.append(
                {
                    "address": meta.get("address", ""),
                    "document": doc,
                    "score": round(1.0 - dist, 4),
                }
            )
        return out

    # ------------------------------------------------------------------ internals

    def _stored(self, source_name: str) -> tuple[dict[str, str], dict[str, list[str]]]:
        """Return {address: md5} and {address: [chunk_ids]} for a source's chunks."""
        stored = self._collection.get(where={"source": source_name}, include=["metadatas"])
        md5_by_addr: dict[str, str] = {}
        ids_by_addr: dict[str, list[str]] = {}
        for doc_id, meta in zip(stored["ids"], stored["metadatas"]):
            addr = meta.get("address", "")
            md5_by_addr[addr] = meta.get("md5", "")
            ids_by_addr.setdefault(addr, []).append(doc_id)
        return md5_by_addr, ids_by_addr

    def _source_files(self, source: Source) -> dict[str, str]:
        """Map each indexable file under a source to its address and text."""
        files: dict[str, str] = {}
        root = source.root.resolve()
        for path in root.rglob("*"):
            if not path.is_file() or path.suffix.lstrip(".").lower() not in INDEX_FILE_TYPES:
                continue
            try:
                text = path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            if text.strip():
                rel = path.relative_to(root).as_posix()
                files[f"{source.name}:{rel}"] = text
        return files

    def _index(self, source_name: str, addr: str, text: str, md5: str) -> None:
        docs = self._documents(addr, text)
        if not docs:
            return
        ids = [f"{addr}::chunk{i}" for i in range(len(docs))]
        metadatas: list[dict[str, Any]] = [
            {"source": source_name, "address": addr, "chunk_index": i, "md5": md5}
            for i in range(len(docs))
        ]
        self._collection.upsert(ids=ids, documents=docs, metadatas=metadatas)
        logger.debug("indexed %s (%d chunks)", addr, len(docs))

    def _documents(self, addr: str, text: str) -> list[str]:
        """Split a file into embeddable chunks; jsonl is split per turn."""
        if addr.endswith(".jsonl"):
            docs: list[str] = []
            for line in text.splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    turn = json.loads(line)
                except json.JSONDecodeError:
                    continue
                content = _indexable_content(turn.get("content")).strip()
                if content:
                    docs.append(f"{turn.get('role', '')}: {content}".strip(": "))
            return docs
        words = text.split()
        return [
            " ".join(words[i : i + INDEX_CHUNK_WORDS])
            for i in range(0, len(words), INDEX_CHUNK_WORDS)
            if words[i : i + INDEX_CHUNK_WORDS]
        ]
