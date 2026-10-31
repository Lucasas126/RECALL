# sources & addressing

the data plane (L1) is a set of **sources**: named, typed locations the agent can access. every tool that touches memory goes through this layer.

## source types

| source | owner | access | indexed | overlay |
|---|---|---|---|---|
| `mind_palace` | the agent | read/write | yes | none |
| `conversations` | the system | read (append-only) | yes | none |
| `obsidian` | the user | read/write | yes | safe or auto |
| `user_folder` | the user | read-only | yes | none |

- **mind_palace**: the agent's private space. obsidian-like structure (folders, frontmatter, tags). the user does not normally read it.
- **conversations**: chats with the user, written by the system, never edited by the agent. stored as one JSONL file per chat, `conversations:<chat_id>.jsonl` (one turn per line, timestamped). a chat title is derived from its first user message. assistant turns may carry `reasoning` and `actions` (the tool calls of that turn, with truncated results) for display. the agent can read and search all of its chats. chats are created, appended to and deleted through `ConversationsSource` methods (`create`, `append`, `delete_chat`), not through the generic write/delete. chat ids match `[A-Za-z0-9_-]{1,64}`; new ids look like `20261030-114140-3ad2f3`. older per-day files (`2026-08-31.jsonl`) are valid chats whose id is the day.
- **obsidian**: the user's vault, bound per agent (see agents.md). accessed through the safe/auto overlay (see overlay.md).
- **user_folder**: extra reference material the user drops in. read-only.

Cancelled agent generations append an assistant turn with `interrupted: true`,
the emitted partial `content` (possibly empty), partial `reasoning` and completed
`actions`. The stored content stays unmodified; the agent adds an interruption
marker when using it as context, including after reopening the chat.
`ChatService.get_chat` exposes the flag and the web UI labels interrupted replies. Active
and queued chats are protected from empty-chat pruning. See
[generation cancellation](architecture.md#generation-cancellation).

Each chat may also have a `<chat_id>.model.json` sidecar, managed only through
`ConversationsSource.model_selection` and `set_model`. It stores
`{"provider": "openrouter", "id": "vendor/model", "zdr": true}` separately from message
history, so selecting a model does not add a turn or create another chat.
Missing metadata means the current user's LLM default with ZDR required;
old sidecars without `zdr` also default to true. Explicit false is per chat
and persists across restarts. Invalid metadata is an
explicit error, not a fallback. Writes are atomic and use the source's path
validation. Deleting/pruning a chat removes its sidecar. These `.json` files are
not indexed by the default `txt`, `md`, `jsonl` extension filter.
The STT model is a user setting and has no per-chat sidecar. Deleting/pruning a
chat removes its model and reasoning-effort sidecars.
Both selections are managed through the conversations source, never through
direct channel filesystem access.
New assistant turns also record their model ID, so usage from a different
model is not reused as the current model's input-token count.
See [per-chat model selection](config.md#choosing-a-model-per-chat).

sources are configurable per agent — an agent may have no obsidian, or several user folders.

## addressing

every memory is addressed as `source:relative/path`:

```
mind_palace:hobbies/arte.md
obsidian:projects/recall.md
conversations:2026-08-31.jsonl
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

sources marked `indexed: true` feed the shared vector index. each chunk carries metadata: `agent_id`, `source`, `path`, `md5`, and (for obsidian-like sources) `tags`. the index is **namespaced per agent** so retrieval never crosses agents (see agents.md). the overlay is applied before indexing, so vectors reflect the agent's view, not the raw user files.

## status

implemented: the `SourceRegistry` (address resolution `source:path` + traversal guard), the `mind_palace` and `conversations` sources, and read/write/list/delete over them. source roots are derived by convention as `data/<user_id>/<agent_id>/<source>` (per-agent source config is planned).

planned: the `obsidian` source + safe/auto overlay, the `user_folder` source, and the vector index (`query_memory`, tags) described above.
