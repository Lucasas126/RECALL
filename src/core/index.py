"""Per-agent vector index.

Chunks live in one Chroma collection per agent (`data/<user>/<agent>/.index`); what has been indexed
(file stats and MD5, how far each chat is indexed) lives in a SQLite manifest beside it, so unchanged
files are never read and chats are indexed incrementally.

Concurrency: the web and Telegram processes may share a data folder, and Chroma's `PersistentClient`
is not process-safe (a client does not see another process's writes and its HNSW reader can fail).
Every Chroma access therefore happens inside a short *store* session that holds a cross-process file
lock (`store.lock`); each write bumps a generation number in the manifest, and a process that finds a
generation it did not write drops its cached Chroma client and reopens it from disk. Embedding calls run
outside that lock but under a second cross-process lock (`write.lock`), taken per file or chat, so two
processes never embed the same change twice and searches only wait for the brief Chroma writes.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import sqlite3
import threading
import time
import uuid
from collections import OrderedDict
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

import chromadb

from src.config.models import IndexConfig
from src.config.settings import DATA_DIR, INDEX
from src.core.chunking import CHUNKING_VERSION, Chunk, ChunkSizes, chunk_exchange, chunk_markdown, chunk_skill
from src.core.sources.base import Source, scan_files
from src.core.sources.folders import FolderSource
from src.core.sources.registry import SourceRegistry
from src.providers.cancellation import CancellationToken
from src.providers.provider import LLMProvider
from src.utils.files import NonBlockingFileLock, exclusive_file_lock
from src.utils.logger import get_logger

logger = get_logger("vector-index")

MANIFEST_FILE = "manifest.sqlite3"
_STORE_LOCK = "store.lock"
_WRITE_LOCK = "write.lock"
# file sources: only prose is embedded; `.jsonl` is indexed as conversations only
_TEXT_EXTS = {"md", "txt"}
_IDLE_CHECK_SECONDS = 60.0
_UPSERT_BATCH = 1000
# hybrid search: candidates taken from each ranking, and the reciprocal rank fusion constant
_HYBRID_CANDIDATES = 50
_RRF_K = 60
# keyword table beside the manifest; bump to rebuild it from the stored chunks
_FTS_VERSION = "1"
_FTS_WORD = re.compile(r"\w+", re.UNICODE)
_FTS_MAX_TERMS = 32


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


def chunking_signature(settings: IndexConfig) -> str:
    """The chunking scheme an index was built with: version plus the configured sizes."""
    return f"{CHUNKING_VERSION}:{settings.chunk_tokens}/{settings.max_chunk_tokens}/{settings.overlap_tokens}"


@dataclass(frozen=True)
class IndexIdentity:
    """What makes vectors comparable: the embeddings model and the chunking scheme."""

    model: str
    chunking: str


def _drop_chroma_system(identifier: str) -> None:
    """Forget Chroma's cached client for a path, so the next client reloads it from disk."""
    from chromadb.api.shared_system_client import SharedSystemClient

    systems = getattr(SharedSystemClient, "_identifier_to_system", None)
    if not isinstance(systems, dict):  # private API changed: fall back to clearing every cached client
        SharedSystemClient.clear_system_cache()
        return
    system = systems.pop(identifier, None)
    if system is not None:
        try:
            system.stop()
        except Exception:  # stopping is best effort; the cache entry is gone either way
            logger.debug("could not stop the chroma system for %s", identifier, exc_info=True)


class _Store:
    """One process's handle on an agent's index folder: Chroma client, manifest, locks and worker."""

    def __init__(self, path: Path, collection: str):
        self.path = path
        self.collection_name = collection
        self._identifier = str(path)
        self._guard = threading.RLock()
        self._depth = 0
        self._file_lock = None
        self._client = None
        self._collection = None
        self._seen_generation: int | None = None
        self.identity: IndexIdentity | None = None
        self._write_guard = threading.Lock()
        path.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(path / MANIFEST_FILE, timeout=30, check_same_thread=False, isolation_level=None)
        with self._guard, exclusive_file_lock(path / _STORE_LOCK):
            self._db.executescript(
                "CREATE TABLE IF NOT EXISTS files (address TEXT PRIMARY KEY, source TEXT NOT NULL, mtime_ns INTEGER,"
                " size INTEGER, md5 TEXT, chunks INTEGER NOT NULL DEFAULT 0);"
                "CREATE TABLE IF NOT EXISTS chats (chat_id TEXT PRIMARY KEY, offset INTEGER NOT NULL,"
                " turns INTEGER NOT NULL, mtime_ns INTEGER, size INTEGER, chunks INTEGER NOT NULL DEFAULT 0);"
                "CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);"
                # keyword index of every chunk (BM25), kept in step with the Chroma collection
                "CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(chunk_id UNINDEXED, source UNINDEXED,"
                " address UNINDEXED, chat UNINDEXED, text, tokenize = 'unicode61 remove_diacritics 2');"
            )
        # background work of this process for this index
        self.jobs: OrderedDict[tuple, Callable[[], None]] = OrderedDict()
        self.jobs_changed = threading.Condition(threading.Lock())
        self.running: tuple | None = None
        self.last_error: str | None = None
        self.last_index: "VectorIndex | None" = None  # the periodic folder reconcile uses its sources
        self._worker: threading.Thread | None = None
        self._closed = False

    # ------------------------------------------------------------------ locks

    @contextmanager
    def session(self) -> Iterator["_Store"]:
        """Exclusive access to Chroma and the manifest, across threads and processes (re-entrant)."""
        with self._guard:
            if self._closed:
                raise RuntimeError(f"the index in {self.path} is closed")
            if self._depth == 0:
                lock = exclusive_file_lock(self.path / _STORE_LOCK)
                lock.__enter__()
                self._file_lock = lock
            self._depth += 1
            try:
                if self._depth == 1:
                    generation = self._generation()
                    if self._client is None or generation != self._seen_generation:
                        self._reopen()
                        self._seen_generation = generation
                yield self
            finally:
                self._depth -= 1
                if self._depth == 0:
                    lock, self._file_lock = self._file_lock, None
                    lock.__exit__(None, None, None)

    @contextmanager
    def writing(self) -> Iterator[None]:
        """The writer lock: one thread of one process embeds and stores changes at a time."""
        with self._write_guard:
            lock = NonBlockingFileLock(self.path / _WRITE_LOCK)
            waited = False
            while not lock.acquire():
                if not waited:
                    logger.debug("waiting for another process to finish writing %s", self.path)
                    waited = True
                time.sleep(0.2)
            try:
                yield
            finally:
                lock.release()

    # ------------------------------------------------------------------ chroma

    def _generation(self) -> int:
        return int(self.meta("generation") or 0)

    def bump(self) -> None:
        """Record a write so other processes reload Chroma before their next access."""
        generation = self._generation() + 1
        self.set_meta("generation", str(generation))
        self._seen_generation = generation

    def _reopen(self) -> None:
        if self._client is not None:
            _drop_chroma_system(self._identifier)
        self._client = chromadb.PersistentClient(path=self._identifier)
        self._collection = None
        self.identity = None
        if not self._collection_exists():
            return
        try:
            self._collection = self._client.get_collection(self.collection_name)
        except Exception as exc:  # e.g. an old collection that Chroma cannot open without its embedding function
            logger.warning("could not open the index collection in %s (%s); it will be rebuilt", self.path, exc)
            return
        metadata = self._collection.metadata or {}
        if metadata.get("index_id") and metadata.get("index_id") == self.meta("index_id"):
            if metadata.get("embeddings_model") and metadata.get("chunking_version"):
                self.identity = IndexIdentity(str(metadata["embeddings_model"]), str(metadata["chunking_version"]))

    def _collection_exists(self) -> bool:
        names = [getattr(item, "name", item) for item in self._client.list_collections()]
        return self.collection_name in names

    def recreate(self, identity: IndexIdentity) -> None:
        """Drop every chunk and the manifest, and start an empty collection for `identity` (inside a session)."""
        if self._collection_exists():
            self._client.delete_collection(self.collection_name)
        index_id = uuid.uuid4().hex
        self._collection = self._client.create_collection(
            self.collection_name,
            metadata={
                "hnsw:space": "cosine",  # similarity = 1 - cosine distance, ~[0, 1]
                "embeddings_model": identity.model,
                "chunking_version": identity.chunking,
                "index_id": index_id,
            },
        )
        self._db.executescript("DELETE FROM files; DELETE FROM chats; DELETE FROM chunks_fts;")
        self.set_meta("index_id", index_id)
        self.set_meta("fts_version", _FTS_VERSION)
        self.identity = identity
        self.bump()

    @property
    def collection(self):
        if self._collection is None:
            raise RuntimeError(f"the index in {self.path} has no collection yet")
        return self._collection

    def count(self) -> int:
        return self._collection.count() if self._collection is not None else 0

    # ------------------------------------------------------------------ chunks (Chroma + keyword table)

    def delete_chunks(self, **where: str) -> None:
        """Drop the chunks whose metadata field (`address`, `chat` or `source`) equals the value, in both stores."""
        (field, value), = where.items()
        if field not in {"address", "chat", "source"}:
            raise ValueError(f"cannot delete chunks by {field!r}")
        self.collection.delete(where={field: value})
        self._db.execute(f"DELETE FROM chunks_fts WHERE {field} = ?", (value,))

    def index_keywords(self, ids: list[str], documents: list[str], metadatas: list[dict]) -> None:
        """Add (or replace) chunks in the keyword table."""
        rows = [
            (chunk_id, str(meta.get("source", "")), str(meta.get("address", "")), str(meta.get("chat", "")), doc)
            for chunk_id, doc, meta in zip(ids, documents, metadatas)
        ]
        self._db.executemany("DELETE FROM chunks_fts WHERE chunk_id = ?", [(row[0],) for row in rows])
        self._db.executemany(
            "INSERT INTO chunks_fts (chunk_id, source, address, chat, text) VALUES (?, ?, ?, ?, ?)", rows,
        )

    def keywords_current(self) -> bool:
        return self.meta("fts_version") == _FTS_VERSION

    def rebuild_keywords(self) -> int:
        """Fill the keyword table from the chunks stored in Chroma (no embedding calls); how many were added."""
        self._db.execute("DELETE FROM chunks_fts")
        added = 0
        if self._collection is not None:
            total = self._collection.count()
            for offset in range(0, total, _UPSERT_BATCH):
                page = self._collection.get(include=["documents", "metadatas"], limit=_UPSERT_BATCH, offset=offset)
                self.index_keywords(page["ids"], page["documents"], [meta or {} for meta in page["metadatas"]])
                added += len(page["ids"])
        self.set_meta("fts_version", _FTS_VERSION)
        return added

    def keyword_search(
        self, query: str, limit: int, sources: list[str], exclude_chat: str | None, only_chat: str | None,
    ) -> list[str]:
        """Ids of the best BM25 matches for the words of `query` among the allowed chunks (best first)."""
        match = fts_query(query)
        if not match or not sources:
            return []
        sql = (
            "SELECT chunk_id FROM chunks_fts WHERE chunks_fts MATCH ?"
            f" AND source IN ({', '.join('?' for _ in sources)})"
        )
        params: list = [match, *sources]
        if exclude_chat:
            sql += " AND chat != ?"
            params.append(exclude_chat)
        if only_chat:
            sql += " AND chat = ?"
            params.append(only_chat)
        sql += " ORDER BY bm25(chunks_fts) LIMIT ?"
        params.append(limit)
        try:
            return [row[0] for row in self._db.execute(sql, params)]
        except sqlite3.OperationalError as exc:  # a query FTS cannot parse: the dense ranking still answers
            logger.debug("keyword search failed for %r: %s", match, exc)
            return []

    # ------------------------------------------------------------------ manifest

    def meta(self, key: str) -> str | None:
        row = self._db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str | None) -> None:
        if value is None:
            self._db.execute("DELETE FROM meta WHERE key = ?", (key,))
        else:
            self._db.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))

    def file_rows(self, source: str) -> dict[str, tuple[int, int, str, int]]:
        rows = self._db.execute("SELECT address, mtime_ns, size, md5, chunks FROM files WHERE source = ?", (source,))
        return {address: (mtime, size, md5, chunks) for address, mtime, size, md5, chunks in rows}

    def file_row(self, address: str) -> tuple[int, int, str, int] | None:
        return self._db.execute(
            "SELECT mtime_ns, size, md5, chunks FROM files WHERE address = ?", (address,),
        ).fetchone()

    def save_file(self, address: str, source: str, mtime: int, size: int, md5: str, chunks: int) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO files (address, source, mtime_ns, size, md5, chunks) VALUES (?, ?, ?, ?, ?, ?)",
            (address, source, mtime, size, md5, chunks),
        )

    def file_sources(self) -> list[str]:
        return [row[0] for row in self._db.execute("SELECT DISTINCT source FROM files")]

    def forget_source(self, source: str) -> None:
        self._db.execute("DELETE FROM files WHERE source = ?", (source,))

    def forget_file(self, address: str) -> None:
        self._db.execute("DELETE FROM files WHERE address = ?", (address,))

    def chat_rows(self) -> dict[str, tuple[int, int, int, int, int]]:
        rows = self._db.execute("SELECT chat_id, offset, turns, mtime_ns, size, chunks FROM chats")
        return {chat: (offset, turns, mtime, size, chunks) for chat, offset, turns, mtime, size, chunks in rows}

    def chat_row(self, chat_id: str) -> tuple[int, int, int, int, int] | None:
        return self._db.execute(
            "SELECT offset, turns, mtime_ns, size, chunks FROM chats WHERE chat_id = ?", (chat_id,),
        ).fetchone()

    def save_chat(self, chat_id: str, offset: int, turns: int, mtime: int, size: int, chunks: int) -> None:
        self._db.execute(
            "INSERT OR REPLACE INTO chats (chat_id, offset, turns, mtime_ns, size, chunks) VALUES (?, ?, ?, ?, ?, ?)",
            (chat_id, offset, turns, mtime, size, chunks),
        )

    def forget_chat(self, chat_id: str) -> None:
        self._db.execute("DELETE FROM chats WHERE chat_id = ?", (chat_id,))

    def counts(self) -> tuple[int, int]:
        files = self._db.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        chats = self._db.execute("SELECT COUNT(*) FROM chats").fetchone()[0]
        return files, chats

    # ------------------------------------------------------------------ background jobs

    def submit(self, key: tuple, job: Callable[[], None]) -> None:
        """Queue a job (replacing a queued one with the same key) and make sure the worker runs."""
        with self.jobs_changed:
            if self._closed:
                return
            self.jobs.pop(key, None)
            self.jobs[key] = job
            if self._worker is None or not self._worker.is_alive():
                self._worker = threading.Thread(target=self._work, name=f"index-{self.path.parent.name}", daemon=True)
                self._worker.start()
            self.jobs_changed.notify_all()

    def busy(self) -> bool:
        with self.jobs_changed:
            return bool(self.jobs) or self.running is not None

    def wait_idle(self, timeout: float | None = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + timeout
        with self.jobs_changed:
            while self.jobs or self.running is not None:
                remaining = None if deadline is None else deadline - time.monotonic()
                if remaining is not None and remaining <= 0:
                    return False
                self.jobs_changed.wait(remaining)
        return True

    def _work(self) -> None:
        while True:
            with self.jobs_changed:
                if not self.jobs and not self._closed:
                    self.jobs_changed.wait(_IDLE_CHECK_SECONDS)
                if self._closed:
                    return
                key, job = self.jobs.popitem(last=False) if self.jobs else (None, None)
                self.running = key or ("idle",)
            try:
                if job is not None:
                    job()
                    self.last_error = None
                elif self.last_index is not None:  # idle: rescan the user folders when their interval passed
                    self.last_index.reconcile_folders_if_due()
            except Exception as exc:  # a failed job is logged and retried by a later reconcile
                self.last_error = f"{type(exc).__name__}: {exc}"
                logger.exception("index job %s failed in %s", key, self.path)
            finally:
                with self.jobs_changed:
                    self.running = None
                    self.jobs_changed.notify_all()

    def close(self) -> None:
        with self.jobs_changed:
            self._closed = True
            self.jobs.clear()
            self.jobs_changed.notify_all()
        worker = self._worker
        if worker is not None and worker is not threading.current_thread():
            worker.join(timeout=30)
        with self._guard:
            if self._client is not None:
                _drop_chroma_system(self._identifier)
            self._client = self._collection = None
            self._db.close()


# one handle per index folder per process, shared by every chat and dream of the agent (Chroma itself
# also caches one client per path, so separate handles could not reload it independently)
_STORES: dict[Path, _Store] = {}
_STORES_GUARD = threading.Lock()


def _store_for(path: Path, collection: str) -> _Store:
    key = path.resolve()
    with _STORES_GUARD:
        store = _STORES.get(key)
        if store is None:
            store = _Store(key, collection)
            _STORES[key] = store
        return store


def close_index(path: Path) -> None:
    """Stop the worker of an index folder and release its files (tests, shutdown)."""
    with _STORES_GUARD:
        store = _STORES.pop(path.resolve(), None)
    if store is not None:
        store.close()


class _Embedder:
    def __init__(self, provider: LLMProvider, model: str, batch: int):
        self._provider = provider
        self._model = model
        self._batch = batch

    def __call__(self, texts: list[str], cancellation: CancellationToken | None = None) -> list[list[float]]:
        vectors: list[list[float]] = []
        extra = {} if cancellation is None else {"cancellation": cancellation}
        for start in range(0, len(texts), self._batch):
            vectors.extend(self._provider.invoke_embeddings(texts[start:start + self._batch], self._model, **extra))
        if len(vectors) != len(texts):
            raise RuntimeError(f"the embeddings provider returned {len(vectors)} vectors for {len(texts)} texts")
        return vectors


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class VectorIndex:
    """An agent's semantic index over its indexed sources.

    Searches never sync: they query what is indexed. Changes are indexed in the background by
    `reconcile` (stat-only scan at chat and dream start), `after_turn` (the chat's new exchanges and
    the files a turn wrote) and the periodic folder reconcile.
    """

    def __init__(
        self, path: Path, registry: SourceRegistry, provider: LLMProvider, model: str,
        settings: IndexConfig = INDEX,
    ):
        self._registry = registry
        self._settings = settings
        self._embed = _Embedder(provider, model, settings.embed_batch)
        self.identity = IndexIdentity(model, chunking_signature(settings))
        self._sizes = ChunkSizes(settings.chunk_tokens, settings.max_chunk_tokens, settings.overlap_tokens)
        self._store = _store_for(path, settings.collection)
        self._store.last_index = self
        self.path = self._store.path

    @classmethod
    def for_agent(
        cls, user_id: str, agent_id: str, registry: SourceRegistry, provider: LLMProvider, model: str,
        *, data_dir: Path | None = None,
    ) -> "VectorIndex":
        path = (data_dir or DATA_DIR) / user_id / agent_id / INDEX.dir_name
        return cls(path, registry, provider, model)

    # ------------------------------------------------------------------ scheduling

    def reconcile(self, *, folders: bool | None = None) -> None:
        """Queue a stat-only reconcile of the agent's own sources; folders too when due (None) or always (True)."""
        self._store.last_index = self
        self._store.submit(("reconcile",), self._reconcile_own)
        if folders or (folders is None and self._folders_due()):
            self._store.submit(("folders",), self._reconcile_folders)

    def reindex(self, *, full: bool = False) -> None:
        """Queue a reconcile of every source now; `full` drops the index first and rebuilds it."""
        if full:
            self._store.submit(("rebuild",), lambda: self._rebuild(force=True))
        self.reconcile(folders=True)

    def after_turn(self, chat_id: str, written: Iterable[str] = ()) -> None:
        """Queue indexing of a chat's new exchanges and of the files the turn's tools wrote."""
        self._store.last_index = self
        self._store.submit(("chat", chat_id), lambda: self._index_chat_job(chat_id))
        for address in dict.fromkeys(written):
            self._store.submit(("file", address), lambda address=address: self._index_address(address))

    def forget_chat(self, chat_id: str) -> None:
        """Drop a deleted chat's chunks now instead of waiting for the next reconcile."""
        with self._store.writing(), self._store.session() as store:
            if store.identity == self.identity:
                store.delete_chunks(chat=chat_id)
            store.forget_chat(chat_id)
            store.bump()

    def reconcile_folders_if_due(self) -> None:
        if self._folders_due():
            self._reconcile_folders()

    def wait(self, timeout: float | None = None) -> bool:
        """Block until this process has no queued or running work on this index; False on timeout."""
        return self._store.wait_idle(timeout)

    def sync(self) -> None:
        """Reconcile every indexed source now, in the calling thread (folders included)."""
        self._reconcile_own()
        self._reconcile_folders()

    def close(self) -> None:
        close_index(self.path)

    # ------------------------------------------------------------------ query

    def query(
        self, query: str, top_k: int = 5, *, exclude_chat: str | None = None, only_source: str | None = None,
        only_chat: str | None = None, cancellation: CancellationToken | None = None,
    ) -> list[dict]:
        """The `top_k` best chunks as {address, source, context, document, body, score}; never syncs.

        Hybrid search: the nearest chunks by embedding and the best keyword (BM25) matches are merged by
        reciprocal rank fusion, which decides the order. `score` is always the cosine similarity to the query,
        also for chunks found only by keyword, so callers can keep gating on it (fused ranks have no scale).
        `only_source` / `only_chat` narrow the search to one source or one chat's chunks. A `cancellation` token
        aborts the query embedding request in flight (GenerationCancelled)."""
        with self._store.session() as store:
            if store.identity != self.identity or not store.count():
                return []
        vector = self._embed([query], cancellation)[0]
        with self._store.session() as store:
            count = store.count()
            if store.identity != self.identity or not count:
                return []
            where = self._query_filter(exclude_chat, only_source, only_chat)
            if not where:  # the requested source is not visible: nothing to find
                return []
            pool = max(top_k, _HYBRID_CANDIDATES)
            res = store.collection.query(
                query_embeddings=[vector],
                n_results=min(pool, count),
                where=where,
                include=["documents", "distances", "metadatas"],
            )
            chunks: dict[str, dict] = {}
            dense: list[str] = []
            for chunk_id, doc, dist, meta in zip(
                res["ids"][0], res["documents"][0], res["distances"][0], res["metadatas"][0],
            ):
                chunks[chunk_id] = _hit(doc, meta, 1.0 - dist)
                dense.append(chunk_id)
            sources = [only_source] if only_source else _allowed_sources(where)
            keyword = store.keyword_search(query, pool, sources, exclude_chat, only_chat)
            missing = [chunk_id for chunk_id in keyword if chunk_id not in chunks]
            if missing:  # found only by keyword: score them by cosine from their stored vectors
                extra = store.collection.get(ids=missing, include=["documents", "metadatas", "embeddings"])
                for chunk_id, doc, meta, emb in zip(
                    extra["ids"], extra["documents"], extra["metadatas"], extra["embeddings"],
                ):
                    chunks[chunk_id] = _hit(doc, meta, _cosine(vector, emb))
        fused: dict[str, float] = {}
        for ranking in (dense, [chunk_id for chunk_id in keyword if chunk_id in chunks]):
            for rank, chunk_id in enumerate(ranking, start=1):
                fused[chunk_id] = fused.get(chunk_id, 0.0) + 1.0 / (_RRF_K + rank)
        order = sorted(fused, key=lambda chunk_id: (-fused[chunk_id], -chunks[chunk_id]["score"]))
        return [chunks[chunk_id] for chunk_id in order[:top_k]]

    def _query_filter(self, exclude_chat: str | None, only_source: str | None = None, only_chat: str | None = None) -> dict:
        """Chunks the caller may see: own sources, and folders that are readable (and indexed) right now."""
        readable = [
            source.name for source in self._registry.indexed_sources()
            if not isinstance(source, FolderSource) or source.readable
        ]
        own = [name for name in self._registry.source_names() if name not in {f.name for f in self._registry.folders()}]
        allowed = sorted({*readable, *own})
        if only_source and only_source not in allowed:
            return {}
        by_source: dict = {"source": {"$in": [only_source] if only_source else allowed}}
        clauses = [by_source]
        if exclude_chat:
            clauses.append({"chat": {"$ne": exclude_chat}})
        if only_chat:
            clauses.append({"chat": {"$eq": only_chat}})
        return clauses[0] if len(clauses) == 1 else {"$and": clauses}

    # ------------------------------------------------------------------ status

    def status(self) -> dict:
        """What is indexed: sources, files, chats, chunks, last reconciles and whether work is pending."""
        with self._store.session() as store:
            files, chats = store.counts()
            current = store.identity == self.identity
            chunks = store.count() if current else 0
            reconciled = store.meta("reconciled_at")
            folders_at = store.meta("folders_reconciled_at")
            # a missing collection is an index not built yet; one with another identity is being replaced
            rebuilding = (store._collection is not None and not current) or "1" in (
                store.meta("rebuild_own"), store.meta("rebuild_folders"),
            )
        return {
            "sources": [source.name for source in self._registry.indexed_sources()],
            "files": files,
            "chats": chats,
            "chunks": chunks,
            "last_reconcile": reconciled,
            "folders_reconciled_at": folders_at,
            "rebuilding": rebuilding,
            "busy": self._store.busy(),
            "model": self.identity.model,
            "error": self._store.last_error,
        }

    # ------------------------------------------------------------------ reconcile jobs

    def _indexed_folders(self) -> list[FolderSource]:
        return [source for source in self._registry.folders() if source.indexed]

    def _folders_due(self) -> bool:
        if not self._indexed_folders():
            return False
        with self._store.session() as store:
            last = store.meta("folders_reconciled_at_epoch")
        return last is None or time.time() - float(last) >= self._settings.folders_reconcile_minutes * 60

    def _matches(self) -> bool:
        with self._store.session() as store:
            return store.identity == self.identity

    def _ensure_identity(self) -> None:
        if not self._matches():
            self._rebuild(force=False)
        self._ensure_keywords()

    def _ensure_keywords(self) -> None:
        """Build the keyword table of an index made before hybrid search, from the stored chunks."""
        with self._store.session() as store:
            if store.keywords_current():
                return
        with self._store.writing(), self._store.session() as store:
            if store.keywords_current() or store.identity != self.identity:
                return
            added = store.rebuild_keywords()
            store.bump()
        logger.info("built the keyword index of %s from %d stored chunk(s)", self.path, added)

    def _rebuild(self, *, force: bool) -> None:
        """Start an empty collection for this identity; the reconciles that follow fill it again."""
        with self._store.writing(), self._store.session() as store:
            if not force and store.identity == self.identity:
                return
            logger.info("rebuilding the index in %s for %s (chunking %s)", store.path, *vars(self.identity).values())
            store.recreate(self.identity)
            store.set_meta("rebuild_own", "1")
            store.set_meta("rebuild_folders", "1" if self._indexed_folders() else None)
            store.set_meta("folders_reconciled_at_epoch", None)

    def _purge_stale_folders(self) -> None:
        """Drop the chunks of folders no longer configured, no longer indexed, or refused as unsafe, and of any
        other source no longer registered (e.g. `recall_docs` once `recall_knowledge` is off).

        A configured folder that is only missing for now (unmounted drive) keeps its chunks.
        """
        keep = {source.name for source in self._indexed_folders()} | set(self._registry.unavailable_folders)
        own = set(self._registry.source_names()) - {f.name for f in self._registry.folders()}
        with self._store.writing(), self._store.session() as store:
            stale = [name for name in store.file_sources() if name not in own and name not in keep]
            if not stale or store.identity != self.identity:
                return
            for name in stale:
                logger.info("dropping the indexed chunks of source %r: it is no longer an indexed source", name)
                store.delete_chunks(source=name)
                store.forget_source(name)
            store.bump()

    def _reconcile_own(self) -> None:
        self._ensure_identity()
        self._purge_stale_folders()
        for source in self._registry.indexed_sources():
            if isinstance(source, FolderSource):
                continue
            if source.name == "conversations":
                self._reconcile_chats(source)
            else:
                self._reconcile_files(source)
        with self._store.session() as store:
            store.set_meta("reconciled_at", _now_iso())
            store.set_meta("rebuild_own", None)
            folders_pending = store.meta("rebuild_folders") == "1"
            if not self._indexed_folders():
                store.set_meta("rebuild_folders", None)
        if folders_pending and self._indexed_folders():  # a rebuild is not finished until the folders are back
            self._reconcile_folders()

    def _reconcile_folders(self) -> None:
        self._ensure_identity()
        for source in self._indexed_folders():
            self._reconcile_files(source)
        with self._store.session() as store:
            store.set_meta("folders_reconciled_at", _now_iso())
            store.set_meta("folders_reconciled_at_epoch", str(time.time()))
            store.set_meta("rebuild_folders", None)

    # -- files

    def _reconcile_files(self, source: Source) -> None:
        if not source.root.is_dir():  # e.g. an unmounted drive: keep its chunks rather than dropping them
            logger.warning("skipping index reconcile of %s: folder not found: %s", source.name, source.root)
            return
        scanned = source.index_scan(_TEXT_EXTS)
        with self._store.session() as store:
            rows = store.file_rows(source.name)
        for rel, stat in scanned.items():
            row = rows.get(f"{source.name}:{rel}")
            if row is None or (row[0], row[1]) != stat:
                self._index_file(source, rel)
        for address in set(rows) - {f"{source.name}:{rel}" for rel in scanned}:
            self._remove_file(address)

    def _index_address(self, address: str) -> None:
        """Index (or drop) one file a tool wrote, moved or deleted."""
        if not self._matches():
            return
        try:
            source, rel = self._registry.resolve(address)
        except (KeyError, ValueError):
            return
        rel = rel.strip().strip("/")
        if not source.indexed or source.name == "conversations" or Path(rel).suffix.lstrip(".").lower() not in _TEXT_EXTS:
            return
        if source.name == "agent" and not rel.startswith("skills/"):
            return
        if any(part.startswith(".") for part in rel.split("/")):
            return
        self._index_file(source, rel)

    def _index_file(self, source: Source, rel: str) -> None:
        address = f"{source.name}:{rel}"
        with self._store.writing():
            try:
                path = source.resolve(rel)
                info = path.stat()
            except (FileNotFoundError, ValueError):
                self._remove_file_locked(address)
                return
            if not path.is_file():
                self._remove_file_locked(address)
                return
            stat = (info.st_mtime_ns, info.st_size)
            with self._store.session() as store:
                row = store.file_row(address)
            if row is not None and (row[0], row[1]) == stat:
                return
            data = path.read_bytes()
            md5 = hashlib.md5(data).hexdigest()
            if row is not None and row[2] == md5:
                with self._store.session() as store:
                    store.save_file(address, source.name, *stat, md5, row[3])
                return
            chunks = self._file_chunks(source, rel, address, data.decode("utf-8", errors="ignore"))
            vectors = self._embed([chunk.text for chunk in chunks]) if chunks else []
            with self._store.session() as store:
                if store.identity != self.identity:
                    return
                store.delete_chunks(address=address)
                if chunks:
                    _upsert(
                        store,
                        [f"{address}::chunk{i}" for i in range(len(chunks))],
                        [chunk.text for chunk in chunks],
                        vectors,
                        [
                            {"source": source.name, "address": address, "chat": "", "context": chunk.context,
                             "chunk_index": i}
                            for i, chunk in enumerate(chunks)
                        ],
                    )
                store.save_file(address, source.name, *stat, md5, len(chunks))
                store.bump()
        logger.debug("indexed %s (%d chunks)", address, len(chunks))

    def _file_chunks(self, source: Source, rel: str, address: str, text: str) -> list[Chunk]:
        if not text.strip():
            return []
        if source.name == "agent" and rel.endswith("/SKILL.md"):
            return chunk_skill(text, address, self._sizes)
        if source.name == "agent":  # a skill's supporting file: name the skill in the prefix
            parts = rel.split("/")
            return chunk_markdown(text, address, self._sizes, title=f"skill {parts[1]}" if len(parts) > 2 else None)
        return chunk_markdown(text, address, self._sizes)

    def _remove_file(self, address: str) -> None:
        with self._store.writing():
            self._remove_file_locked(address)

    def _remove_file_locked(self, address: str) -> None:
        with self._store.session() as store:
            if store.file_row(address) is None:
                return
            if store.identity == self.identity:
                store.delete_chunks(address=address)
            store.forget_file(address)
            store.bump()

    # -- conversations

    def _reconcile_chats(self, source: Source) -> None:
        # disposable chats are never indexed; a chat that became disposable loses its chunks below
        disposable = _disposable_chats(source)
        scanned = {
            rel[:-len(".jsonl")]: stat
            for rel, stat in scan_files(source.root.resolve(), {"jsonl"}).items()
            if "/" not in rel and rel[:-len(".jsonl")] not in disposable
        }
        with self._store.session() as store:
            rows = store.chat_rows()
        for chat_id, stat in scanned.items():
            row = rows.get(chat_id)
            if row is None or (row[2], row[3]) != stat:
                self._index_chat(source, chat_id)
        for chat_id in set(rows) - set(scanned):
            with self._store.writing(), self._store.session() as store:
                if store.identity == self.identity:
                    store.delete_chunks(chat=chat_id)
                store.forget_chat(chat_id)
                store.bump()

    def _index_chat_job(self, chat_id: str) -> None:
        if self._matches():
            self._index_chat(self._registry.conversations(), chat_id)

    def _index_chat(self, source: Source, chat_id: str) -> None:
        """Embed the exchanges appended to a chat since it was last indexed."""
        with self._store.writing():
            if source.resolve(f"{chat_id}.disposable.json").is_file():
                return
            path = source.resolve(f"{chat_id}.jsonl")
            try:
                info = path.stat()
            except FileNotFoundError:
                return
            stat = (info.st_mtime_ns, info.st_size)
            with self._store.session() as store:
                row = store.chat_row(chat_id)
            if row is not None and (row[2], row[3]) == stat:
                return
            offset, turns, chunks_before = (row[0], row[1], row[4]) if row is not None else (0, 0, 0)
            restart = row is not None and info.st_size < offset
            if restart:  # the chat was rewritten: index it again from the start
                offset, turns, chunks_before = 0, 0, 0
            with path.open("rb") as handle:
                handle.seek(offset)
                data = handle.read()
            exchanges, consumed_bytes, consumed_lines = _complete_exchanges(data, turns)
            title = _chat_title(source, chat_id, path) if exchanges else ""
            documents: list[tuple[str, Chunk, str]] = []
            for line, user, assistant, ts in exchanges:
                context = f"{title} ({ts[:10]})" if title and ts else (title or ts[:10])
                prefix = " > ".join(part for part in (f"conversations:{chat_id}", context) if part)
                for part, chunk in enumerate(chunk_exchange(prefix, user, assistant, self._sizes)):
                    chunk_id = f"conversations:{chat_id}::turn{line}" + (f".{part}" if part else "")
                    documents.append((chunk_id, chunk, context))
            vectors = self._embed([chunk.text for _, chunk, _ in documents]) if documents else []
            with self._store.session() as store:
                if store.identity != self.identity:
                    return
                if restart:
                    store.delete_chunks(chat=chat_id)
                if documents:
                    _upsert(
                        store,
                        [chunk_id for chunk_id, _, _ in documents],
                        [chunk.text for _, chunk, _ in documents],
                        vectors,
                        [
                            {"source": "conversations", "address": f"conversations:{chat_id}.jsonl", "chat": chat_id,
                             "context": context}
                            for _, _, context in documents
                        ],
                    )
                store.save_chat(
                    chat_id, offset + consumed_bytes, turns + consumed_lines, *stat, chunks_before + len(documents),
                )
                store.bump()
        if documents:
            logger.debug("indexed %d new chunk(s) of chat %s", len(documents), chat_id)


def _disposable_chats(source: Source) -> set[str]:
    """Ids of the chats flagged disposable by their `<chat_id>.disposable.json` sidecar."""
    suffix = ".disposable.json"
    return {path.name[: -len(suffix)] for path in source.root.iterdir() if path.name.endswith(suffix)}


def fts_query(text: str) -> str:
    """An FTS5 MATCH expression for the words of `text`: each word quoted, any of them may match."""
    words = list(dict.fromkeys(word.lower() for word in _FTS_WORD.findall(text) if len(word) > 1))
    return " OR ".join(f'"{word}"' for word in words[:_FTS_MAX_TERMS])


def _allowed_sources(where: dict) -> list[str]:
    """The source names of a `_query_filter` result."""
    clauses = where.get("$and", [where])
    for clause in clauses:
        if "source" in clause:
            return list(clause["source"]["$in"])
    return []


def _hit(doc: str, meta: dict | None, score: float) -> dict:
    meta = meta or {}
    return {
        "address": meta.get("address", ""),
        "source": meta.get("source", ""),
        "context": meta.get("context", ""),
        "document": doc,
        "body": doc.split("\n\n", 1)[1] if "\n\n" in doc else doc,
        "score": round(score, 4),
    }


def _cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    norm = math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b))
    return dot / norm if norm else 0.0


def _upsert(store: _Store, ids: list[str], documents: list[str], vectors: list, metadatas: list[dict]) -> None:
    """Store chunks in batches small enough for Chroma's maximum batch size."""
    for start in range(0, len(ids), _UPSERT_BATCH):
        end = start + _UPSERT_BATCH
        store.collection.upsert(
            ids=ids[start:end], documents=documents[start:end], embeddings=vectors[start:end],
            metadatas=metadatas[start:end],
        )
    store.index_keywords(ids, documents, metadatas)


def _complete_exchanges(data: bytes, first_line: int) -> tuple[list[tuple[int, str, str, str]], int, int]:
    """Group whole JSONL lines into finished exchanges: a user turn plus the replies that follow it.

    Returns the exchanges as (line number of the user turn, user text, reply text, timestamp) and how
    many bytes and lines they cover; a trailing user turn still waiting for its reply is left for later.
    """
    lines: list[tuple[int, int, dict | None]] = []  # (line number, byte offset after it, turn)
    position, number = 0, first_line
    while (end := data.find(b"\n", position)) != -1:
        raw = data[position:end]
        position = end + 1
        try:
            turn = json.loads(raw) if raw.strip() else None
        except (json.JSONDecodeError, UnicodeDecodeError):
            turn = None
        lines.append((number, position, turn if isinstance(turn, dict) else None))
        number += 1

    exchanges: list[tuple[int, str, str, str]] = []
    consumed = (0, 0)
    current: dict | None = None

    def finish() -> None:
        nonlocal consumed
        if current is not None and current["end"] is not None:
            user, reply = current["user"], "\n\n".join(text for text in current["replies"] if text)
            if user or reply:
                exchanges.append((current["line"], user, reply, current["ts"]))
            consumed = current["end"]

    for number, end, turn in lines:
        role = turn.get("role") if turn else None
        if role == "user":
            finish()
            current = {"line": number, "user": _indexable_content(turn.get("content")).strip(), "replies": [],
                       "ts": str(turn.get("ts") or ""), "end": None}
        elif role == "assistant":
            if current is None:
                current = {"line": number, "user": "", "replies": [], "ts": str(turn.get("ts") or ""), "end": None}
            current["replies"].append(_indexable_content(turn.get("content")).strip())
            current["end"] = (end, number + 1 - first_line)
        elif current is None:
            consumed = (end, number + 1 - first_line)
    finish()
    return exchanges, consumed[0], consumed[1]


def _chat_title(source: Source, chat_id: str, path: Path) -> str:
    """A chat's stored title, else the start of its first user message, for the chunk prefix."""
    try:
        title = json.loads(source.resolve(f"{chat_id}.title.json").read_text(encoding="utf-8")).get("title")
        if isinstance(title, str) and title.strip():
            return title.strip()
    except (OSError, ValueError, AttributeError):
        pass
    try:
        with path.open("r", encoding="utf-8") as handle:
            for line in handle:
                turn = json.loads(line) if line.strip() else None
                if isinstance(turn, dict) and turn.get("role") == "user":
                    text = " ".join(_indexable_content(turn.get("content")).split())
                    return text[:59] + "…" if len(text) > 60 else text
    except (OSError, ValueError):
        pass
    return ""
