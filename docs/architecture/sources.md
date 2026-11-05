# sources & addressing

the data plane (L1) is a set of **sources**: named, typed locations the agent can access. every tool that touches memory goes through this layer.

## source types

| source | owner | access | indexed | overlay |
|---|---|---|---|---|
| `mind_palace` | the agent | read/write | yes | none |
| `conversations` | the system | read (append-only) | yes | none |
| `agent` | the agent | read (writes through its methods) | `skills/` only | none |
| `dream` | the system | none for tools | no | none |
| `<folder name>` | the user | read with `folders_read`, read/write with `folders_edit` unless `read_only` | when `indexed` (default) | none (overlay planned) |
| `obsidian` | the user | read/write | yes | safe or auto |

- **mind_palace**: the agent's private space. obsidian-like structure (folders, frontmatter, tags). the user does not normally read it.
- **conversations**: chats with the user, written by the system, never edited by the agent. stored as one JSONL file per chat, `conversations:<chat_id>.jsonl` (one turn per line, timestamped). a chat title is derived from its first user message. assistant turns may carry `reasoning` and `actions` (the tool calls of that turn, with truncated results) for display. turns with thinking or tool calls also store `timeline`, the order in which they happened: a list of segments `{"type": "reasoning"|"text", "text": ...}` and `{"type": "tool", "index": n}` (an index into `actions`, so tool payloads are not duplicated). `content`, `reasoning` and `actions` stay as before for every other reader; the web UI uses `timeline` to draw thinking, tool steps and text in order, and older turns without it keep the old layout. the agent can read and search all of its chats. chats are created, appended to and deleted through `ConversationsSource` methods (`create`, `append`, `delete_chat`), not through the generic write/delete. chat ids match `[A-Za-z0-9_-]{1,64}`; new ids look like `20261030-114140-3ad2f3`. older per-day files (`2026-08-31.jsonl`) are valid chats whose id is the day.
- **agent**: the agent's own files, rooted at `data/<user_id>/<agent_id>/` but limited to `SOUL.md`, `soul_history/`, `memories/` and `skills/` (`AgentFilesSource`). tools read it as `agent:SOUL.md`, `agent:skills/<name>/SKILL.md`, ...; changes go through the `self` tools and the web API, which keep the SOUL history, memory limits and skill frontmatter valid. only `skills/` is indexed (read-only through the index), so a relevant skill surfaces by itself; `SOUL.md` and the memory files are always in the system prompt and are not indexed.
- **dream**: dream mode bookkeeping in `dream/` (`review_state.json`, `sessions/`, `.lock`), used by the dream runner and the service only (`DreamStateSource`); listing or reading it through tools is refused.
- **user folders** (`FolderSource`): each entry of the agent's `folders:` list (name, path, `indexed`, `read_only`) is a source named after the folder, e.g. `notes:Projects/plan.md`. every configured folder is registered, so indexed ones feed the index; tools can read them only with a folder tool group (`folders_read` / `folders_edit` in chats, the `folders` dream tool group in dreams) and write them only with `folders_edit` in chats, never in dreams and never when `read_only`. the folder must exist; hidden entries are skipped by listings, searches, reads and the index. see [user folders](../user-guide/memory.md#user-folders).
- **obsidian**: the user's vault, bound per agent (see agents.md). accessed through the safe/auto overlay (see overlay.md). planned; a user folder pointing at the vault is the implemented way to use one today.

Cancelled agent generations append an assistant turn with `interrupted: true`,
the emitted partial `content` (possibly empty), partial `reasoning` and completed
`actions`. The stored content stays unmodified; the agent adds an interruption
marker when using it as context, including after reopening the chat.
`ChatService.get_chat` exposes the flag and the web UI labels interrupted replies. Active
and queued chats are protected from empty-chat pruning. See
[generation cancellation](overview.md#generation-cancellation).

Each chat may also have a `<chat_id>.model.json` sidecar, managed only through
`ConversationsSource.model_selection` and `set_model`. It stores
`{"provider": "openrouter", "id": "vendor/model"}` (plus `"zdr": true|false` when the
chat's policy differs from the user's `require_zdr`) separately from message
history, so selecting a model does not add a turn or create another chat.
A `<chat_id>.title.json` sidecar (`{"title": "..."}`) stores the chat title once the
chat reaches `chat_naming.min_messages` or the user renames it (`rename`); `summary` uses it
instead of the first user message, and a generated title never replaces an existing one.
The sidecar records `"generated": true` for generated titles so that rewinding the first
message drops only those. `truncate_from` rewinds a chat (see
[editing an earlier message](../user-guide/memory.md#editing-an-earlier-message)): it rewrites
the JSONL atomically and appends the removed turns to `<chat_id>.removed.json`, which is not
a chat file and is removed by `delete_chat`.
A `<chat_id>.disposable.json` sidecar (`{"created_at": ...}`, never a `.jsonl`) marks a
[disposable chat](../user-guide/memory.md#disposable-chats): `ConversationsSource.is_disposable`,
`disposable_chats`, `set_disposable`, `expires_at` (last activity, the transcript's mtime, plus
the retention) and `expired_disposable`; `summary` reports `disposable` and `expires_at`.
`ReviewQueue` skips these chats, the index skips them (and drops the chunks of a chat that became
disposable on the next reconcile or at once via `forget_chat`), `build_tools(disposable=True)`
omits the write tools, and `ChatService` deletes expired ones lazily and from the scheduler.
Missing metadata means the current user's LLM default; sidecars without `zdr`
follow the user's `require_zdr`, and a stored flag is per chat and persists
across restarts. Invalid metadata is an
explicit error, not a fallback. Writes are atomic and use the source's path
validation. Deleting/pruning a chat removes its sidecar. These `.json` files are
not indexed by the default `txt`, `md`, `jsonl` extension filter.
The STT model is a user setting and has no per-chat sidecar. Deleting/pruning a
chat removes its model and reasoning-effort sidecars.
Both selections are managed through the conversations source, never through
direct channel filesystem access.
New assistant turns also record their model ID, so usage from a different
model is not reused as the current model's input-token count.
See [per-chat model selection](../user-guide/models-and-providers.md#choosing-a-model-per-chat).

sources are configurable per agent — an agent may have no obsidian, or several user folders.

## addressing

every memory is addressed as `source:relative/path`:

```
mind_palace:hobbies/arte.md
obsidian:projects/recall.md
conversations:2026-08-31.jsonl
notes:Projects/plan.md
```

- the prefix before `:` is the source name; the rest is a path relative to that source's root.
- tools that target one file (`read`, `write`, `edit`, `move`, `delete`) take a full `memory_path` in this form.
- tools that scan (`list`, `search`, `query_memory`, `list_tags`, `find_notes_by_tag`) take an optional `source` filter, which is just the source name (`mind_palace`, `obsidian`, ...); with none they span all sources.
- `move`'s `new_path` must stay in the **same** source as its `memory_path`.

## SourceRegistry

each agent gets its own `SourceRegistry`, built from that agent's config. it is the single entry point to L1 and is injected into every capability and tool.

responsibilities:
- **hold** the configured sources for this agent (isolated from other agents).
- **resolve** a `source:path` string into a real, validated filesystem path.
- **guard** against path traversal: the resolved path must stay inside the source root, or resolution fails.
- **expose** typed accessors (`mind_palace()`, `obsidian()`, `conversations()`, `vector()`), each returning a source handle with its declared capabilities (`readable`, `writable`, `indexed`, `overlay`).
- **route** writes/reads through the overlay when the source declares one.

a capability never opens files directly — it always asks the registry, so isolation, overlay and path-safety are enforced in one place.

## indexing

sources marked `indexed` feed the agent's vector index (`VectorIndex`, one Chroma collection per agent in `data/<user_id>/<agent_id>/.index`, so retrieval never crosses agents): the mind palace, `agent:skills/`, the conversations and the indexed user folders. each chunk carries `source`, `address`, `context` (heading path, or chat title and date) and, for chats, `chat`. what has been indexed (each file's `(mtime, size)` and MD5, each chat's indexed byte offset and turn count) is kept in `.index/manifest.sqlite3`, so a reconcile only stats files and a chat only embeds its new exchanges (chunk ids `conversations:<chat_id>::turn<N>`). the index reads files through the source's path guard (`Source.index_scan`, `Source.resolve`), not through the tools' read permission, so a folder with no folder tool group is still indexed. see [memory index](../user-guide/memory.md#memory-index) for the chunking, the update schedule and cross-process locking. planned: tags in chunk metadata, and indexing obsidian through the overlay so vectors reflect the agent's view.

## status

implemented: the `SourceRegistry` (address resolution `source:path` + traversal guard), the `mind_palace`, `conversations`, `agent`, `dream` and user folder sources, read/write/list/delete over them, and the vector index (`query_memory`, associative memory). `list_memories` without a source lists only readable sources. the agent's own source roots are derived by convention as `data/<user_id>/<agent_id>/<source>`; user folders come from the agent's `folders:` list.

planned: the `obsidian` source + safe/auto overlay, and tags in the index.
