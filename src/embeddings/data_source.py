from __future__ import annotations

import hashlib
import os
from typing import Any, Optional

import chromadb
import chromadb.utils.embedding_functions as ef
import requests
from pydantic import BaseModel, Field, model_validator

from src.providers.provider import LLMProvider


# ---------------------------------------------------------------------------
# Chroma client factory
# ---------------------------------------------------------------------------

def _make_chroma_client(save_path: Optional[str]) -> chromadb.ClientAPI:
    """Return a persistent client if *save_path* is given, else an in-memory one."""
    if save_path:
        os.makedirs(save_path, exist_ok=True)
        return chromadb.PersistentClient(path=save_path)
    return chromadb.Client()

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _file_md5(path: str) -> str:
    """Return the MD5 hex-digest of a file's contents."""
    h = hashlib.md5()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _chunk_text(text: str, chunk_size: int) -> list[str]:
    """Split *text* into non-overlapping chunks of *chunk_size* words."""
    words = text.split()
    return [
        " ".join(words[i : i + chunk_size])
        for i in range(0, len(words), chunk_size)
        if words[i : i + chunk_size]
    ]


# ---------------------------------------------------------------------------
# ChromaDB embedding function wrapper
# ---------------------------------------------------------------------------

class _ProviderEmbeddingFunction(ef.EmbeddingFunction):
    """Wraps an LLMProvider.invoke_embeddings method so ChromaDB accepts it."""

    def __init__(self, provider: Any, model: str):
        self._provider = provider
        self._model = model

    def name(self) -> str:  # required by ChromaDB
        return f"provider-{self._model}"

    def __call__(self, input: list[str]) -> list[list[float]]:
        return [self._provider.invoke_embeddings(text, self._model) for text in input]


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

class DataSourceConfig(BaseModel):
    """Configuration for a data source."""

    embeddings_model: str = "openai/text-embedding-3-small"
    chunk_size: int = 100
    file_types: list[str] = ["txt", "md", "jsonl"]
    collection_save_path: Optional[str] = None
    api_key: Optional[str] = None


# ---------------------------------------------------------------------------
# DataSource
# ---------------------------------------------------------------------------

class DataSource(BaseModel):
    """
    Represents a data source for embeddings.
    Uses ChromaDB to index files and generate embeddings.
    Embeddings are stored in a local vector database for retrieval.
    """

    name: str
    path: str
    config: DataSourceConfig = Field(default_factory=DataSourceConfig)
    embeddings_provider: Any = Field(default_factory=LLMProvider)

    # Private fields – excluded from serialisation
    _client: Any = None
    _collection: Any = None
    _embedding_fn: Any = None

    model_config = {"arbitrary_types_allowed": True}

    @model_validator(mode="after")
    def _setup(self) -> "DataSource":
        self._embedding_fn = _ProviderEmbeddingFunction(
            self.embeddings_provider, self.config.embeddings_model
        )
        self._client = _make_chroma_client(self.config.collection_save_path)
        existed = self.name in {
            c.name for c in self._client.list_collections()
        }
        self._collection = self._client.get_or_create_collection(
            name=self.name,
            embedding_function=self._embedding_fn,
        )
        if existed:
            self._sync()
        else:
            self._index_all()
        return self

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def index(self) -> None:
        """Re-index all files in the data source path."""
        self._index_all()

    def query(self, query: str, top_k: int = 5) -> list[dict]:
        """
        Query the collection and return the *top_k* most relevant chunks.

        Each result is a dict with keys: ``id``, ``document``, ``score``,
        ``file``, ``chunk_index``.
        """
        results = self._collection.query(
            query_texts=[query],
            n_results=min(top_k, self._collection.count() or 1),
            include=["documents", "distances", "metadatas"],
        )
        output: list[dict] = []
        for doc, dist, meta in zip(
            results["documents"][0],
            results["distances"][0],
            results["metadatas"][0],
        ):
            output.append(
                {
                    "document": doc,
                    "score": 1.0 - dist,  # convert L2/cosine distance → similarity
                    "file": meta.get("file", ""),
                    "chunk_index": meta.get("chunk_index", 0),
                }
            )
        return output

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def get_files(self) -> list[str]:
        """Walk *self.path* and return paths matching configured file types."""
        matched: list[str] = []
        for root, _dirs, files in os.walk(self.path):
            for filename in files:
                ext = filename.rsplit(".", 1)[-1].lower()
                if ext in self.config.file_types:
                    matched.append(os.path.join(root, filename))
        return matched

    def _index_all(self) -> None:
        """Index every eligible file from scratch."""
        for file_path in self.get_files():
            self._index_file(file_path)

    def _sync(self) -> None:
        """
        Incremental sync:
        - Re-index files whose MD5 changed since the last indexing.
        - Index new files not yet in the collection.
        - Remove chunks whose source file no longer exists.
        """
        existing_files = set(self.get_files())

        # Fetch all stored metadatas to build a map: file → stored_md5
        stored = self._collection.get(include=["metadatas"])
        stored_file_hashes: dict[str, str] = {}
        stored_ids_by_file: dict[str, list[str]] = {}
        for doc_id, meta in zip(stored["ids"], stored["metadatas"]):
            fp = meta.get("file", "")
            stored_file_hashes[fp] = meta.get("md5", "")
            stored_ids_by_file.setdefault(fp, []).append(doc_id)

        # Remove chunks for deleted files
        deleted_files = set(stored_ids_by_file.keys()) - existing_files
        for fp in deleted_files:
            self._collection.delete(ids=stored_ids_by_file[fp])

        # Index new or changed files
        for fp in existing_files:
            current_md5 = _file_md5(fp)
            if fp not in stored_file_hashes:
                self._index_file(fp)
            elif stored_file_hashes[fp] != current_md5:
                # Delete old chunks then re-index
                self._collection.delete(ids=stored_ids_by_file[fp])
                self._index_file(fp)

    def _index_file(self, file_path: str) -> None:
        """Chunk a single file and upsert its chunks into the collection."""
        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as fh:
                text = fh.read()
        except OSError:
            return

        if not text.strip():
            return

        file_md5 = _file_md5(file_path)
        chunks = _chunk_text(text, self.config.chunk_size)

        ids = [f"{file_path}::chunk{i}" for i in range(len(chunks))]
        metadatas = [
            {"file": file_path, "chunk_index": i, "md5": file_md5}
            for i in range(len(chunks))
        ]

        self._collection.upsert(
            ids=ids,
            documents=chunks,
            metadatas=metadatas,
        )


# ---------------------------------------------------------------------------
# SourcesCollection
# ---------------------------------------------------------------------------

class SourcesCollection(BaseModel):
    """Aggregates multiple DataSource instances for unified querying."""

    sources: list[DataSource] = []

    model_config = {"arbitrary_types_allowed": True}

    def add(self, source: DataSource) -> None:
        """Add a data source to the collection."""
        self.sources.append(source)

    def query(self, query: str, top_k: int = 5) -> list[dict]:
        """
        Query all data sources and return the *top_k* most relevant results
        across all of them, ranked by score.
        """
        results: list[dict] = []
        for source in self.sources:
            results.extend(source.query(query, top_k))
        results.sort(key=lambda x: x["score"], reverse=True)
        return results[:top_k]


