# associative memory v2: sources, indexing and chunking

Status: **implemented** (see [implementation notes](#implementation-notes)). The
behavior is documented in [memory](../user-guide/memory.md#memory-index),
[user folders](../user-guide/memory.md#user-folders), [sources](sources.md#indexing) and
[architecture](overview.md#associative-recall-in-parallel); this page keeps the
design and the decisions taken.

## problems of the original index (solved by this design)

- **whole-chat re-embedding**: a chat is one growing `.jsonl`; every new message
  changes its MD5, so all its chunks are deleted and every turn is embedded again.
- **full scan every turn**: `VectorIndex.sync()` walks and hashes every indexable
  file on each search; fine for a small palace, too slow for an Obsidian vault.
- **chunking**: fixed 100-word windows (~130 tokens), no overlap, blind to
  Markdown structure; chat chunks are single turns ("yes", "ok" carry no meaning).
- **no index identity**: the embeddings model and chunking scheme are not stored,
  so changing either mixes incompatible vectors (or fails on dimensions).
- the chat being answered can come back as a "memory" although it is already in
  the context.

## sources

### the agent's own memory (always on)

- `mind_palace:` (notes, `journal/`, `questions.md`): always a source, always
  indexed, always readable and writable by the agent. Not configurable.
- skills (`agent:skills/**/SKILL.md` and their files): indexed, so a relevant
  skill surfaces by itself. Read-only through the index; edited with the `self`
  tools.
- `SOUL.md`, `MEMORY.md`, `USER.md`: **not indexed**; they are always in the
  system prompt, indexing them would only duplicate hits.
- `conversations:` indexed incrementally (see below); the current chat is always
  excluded from associative results.

### user folders (configurable)

Replaces the single `vault:` path. Per agent, a list of named folders:

```yaml
folders:
  - name: obsidian              # source name -> addresses `obsidian:path/note.md`
    path: /path/to/vault
    indexed: true               # used by associative memory and query_memory (default true)
    read_only: false            # optional hard override: never writable, whatever the tools
  - name: work
    path: D:/projects/notes
```

- each folder is a source with the usual path guard; hidden entries (`.obsidian/`,
  `.git/`) are skipped by listings, searches and the index.
- tools: the `vault_read` / `vault_edit` groups are renamed `folders_read` /
  `folders_edit` and apply to every folder; `folders_edit` writes only to folders
  that are not `read_only`. Tool calls take the folder name as the source prefix.
- dreams: folders are read-only, as today, and only with the `folders` dream tool
  group (renamed from `vault`).
- names must be unique, match `^[a-z0-9_-]{1,32}$` and not clash with built-in
  sources (`mind_palace`, `conversations`, `agent`, `dream`).
- the old `vault: <path>` key is removed (the existing test agents are migrated by
  hand; no migration code).

## indexing

- **per-file change detection** by `(mtime, size)` first, MD5 only when those
  changed; unchanged files are never read.
- **conversations incremental**: a chat stores how many turns are indexed; after
  a reply, only the new turns are embedded. Chunk ids are stable
  (`<chat>::turn<N>`), so nothing is deleted and re-embedded.
- **when**:
  - after each reply, in the background (no added latency): index the chat's new
    turns and any file the turn's tools wrote;
  - at chat start and at the start of a dream: a cheap reconcile (stat only);
  - user folders: reconcile in a background thread with a minimum interval
    (e.g. every 10 minutes) and on demand from the web UI; never on the hot path
    of a turn.
- the associative search itself never triggers a sync; it queries what is
  indexed.
- deleted files and chats are removed from the index on reconcile.

## chunking (tuned for `openai/text-embedding-3-small`)

- **Markdown / text**: split by headings, then by paragraphs, packing into chunks
  of ~400 tokens (max ~600) with ~60 tokens of overlap; never split inside a code
  block when avoidable. Each chunk is prefixed with its context:
  `<source>:<path> > <heading path>` (and the note title), which improves
  retrieval noticeably for short sections.
- **conversations**: a chunk is a user message plus the assistant reply (one
  exchange), with the chat title and date as prefix; very long replies are split
  with the Markdown rules. Tool payloads and attachments are not indexed (as now).
- **skills**: one chunk per section, prefixed with the skill name and description.
- token counts are estimated (chars / 4) unless a tokenizer is already a
  dependency; no new heavy dependency.

## index identity

The collection metadata stores the embeddings model and a `chunking_version`.
When either differs from the current configuration, the agent's index is rebuilt
in the background (old one used until the new one is complete, or searches return
nothing while rebuilding: pick the simpler, documented).

## associative results

- exclude chunks from the current chat;
- keep `top_n` (the application default for the maximum, overridable per user in Settings with
  `associative_max_matches`, 1-20) and the application-only `score_threshold`; show each hit with its address and up to
  ~400 characters (was 200), cut at a sentence boundary when possible.

## web UI

- agent settings: the folders list (name, path, indexed, read-only) replaces the
  single vault field; validation errors for missing paths or bad names.
- per agent, an index status line: sources, files and chunks indexed, last
  reconcile, rebuilding or not, and a "reindex" button.

## out of scope

recall mode, the Obsidian safe/auto overlay, a different embeddings model.

## implementation notes

- **code**: `src/core/chunking.py` (Markdown, skill and exchange chunking, `excerpt`),
  `src/core/index.py` (`VectorIndex`, manifest, locks, background worker),
  `src/core/sources/folders.py` (`FolderSource`), `src/agent/toolsets/folders.py`
  (`folder_*` tools), `FolderConfig` / `IndexConfig` in `src/config/models.py`.
- **manifest**: a SQLite file beside the Chroma data (`.index/manifest.sqlite3`)
  holds per-file `(mtime_ns, size, md5, chunks)`, per-chat `(byte offset, turns,
  mtime_ns, size, chunks)` and a few meta keys (generation, last reconciles, rebuild
  flags, the collection's `index_id`). Chunk ids are `<address>::chunk<i>` for files and
  `conversations:<chat>::turn<N>` (plus `.<k>` for the parts of a split exchange),
  where `N` is the line of the user turn. A chat whose file shrank is reindexed from
  the start; a trailing user turn without a reply waits for the next pass.
- **when**: `Agent.__init__` queues `reconcile()` (own sources stat-only; folders when
  due); `Agent._remember_reply` queues `after_turn(chat_id, written)` with the addresses
  the turn's `write`/`append`/`edit`/`move`/`delete`, `folder_*` and `skill_manage` calls
  touched; dreams queue a reconcile at start and end. A per-index worker thread runs
  the jobs (coalesced by key) and, when idle, checks every minute whether the folders
  are due. `ChatService.reindex` / the web **Reindex** button reconciles everything
  now; `full: true` rebuilds.
- **concurrency** (web and Telegram on one data folder): Chroma's `PersistentClient`
  does not see another process's writes and its HNSW reader can fail on them (checked
  with chromadb 1.5.9: "Error creating hnsw segment reader: Nothing found on disk").
  Every Chroma or manifest access holds `.index/store.lock` (an OS file lock, plus a
  thread lock); each write bumps `generation` in the manifest, and a process that
  sees a generation it did not write drops Chroma's cached client for that path and
  reopens it from disk. Embedding requests run outside that lock, under
  `.index/write.lock`, taken per file or chat, so searches never wait for embeddings
  and two processes never embed the same change twice. One handle per index folder is
  kept per process, shared by every chat and dream of the agent. Dropping Chroma's
  cached client uses its private `SharedSystemClient._identifier_to_system`; if that
  API disappears the code falls back to clearing Chroma's whole client cache.
- **identity**: the collection metadata stores `embeddings_model`,
  `chunking_version` (`<version>:<chunk>/<max>/<overlap>`, so changing a size counts)
  and an `index_id` that must match the manifest. On a mismatch (including indexes
  from before this change) searches return nothing, and the next reconcile drops the
  collection and rebuilds in the background; searches return what the new index
  already holds while it fills ("searches return nothing while rebuilding", the
  simpler option). `rebuilding` stays true until both the own sources and the folders
  were reconciled. Only a reconcile (chat or dream start, reindex) rebuilds; the
  after-turn job of a chat still running with an old configuration skips writing, so
  two processes with different configurations cannot rebuild back and forth.
- **sources**: every configured folder is registered (indexed when `indexed`);
  readable by the tools only with a folder tool group, writable only with
  `folders_edit` and not `read_only`, never in dreams. A folder whose path is missing
  when the agent loads is skipped with a warning (saving still rejects it), and at
  reconcile time it is skipped rather than purged (an unmounted drive keeps its chunks).
- **results**: hits carry `context` (heading path or chat title and date) and `body`
  (the chunk without its prefix line); the hint shows `[address > context]` and
  `excerpt(body, 400)`. The current chat is excluded with a `chat != <id>` filter, so
  the old "skip the message being answered" check and the extra result are gone.
- **deviations**: the idle folder check runs only in processes that opened the
  agent's index since they started. `query_memory` has no `sources` filter yet; like associative recall it
  leaves the current chat out. Deleting a chat drops its chunks at once (a failure there
  is only logged; the next reconcile catches it).

