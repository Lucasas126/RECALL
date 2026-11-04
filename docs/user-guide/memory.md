# memory

What the agent remembers and how it finds things again. Design details are in [associative memory internals](../architecture/associative-memory.md)
and [sources](../architecture/sources.md).

- **mind palace**: a private folder of notes per agent (`data/<user_id>/<agent_id>/mind_palace/`), written by the agent itself.
  The user does not normally read it. Always indexed.
- **conversations**: every chat is kept as a JSONL file and indexed one exchange per chunk.
- **personality and short memories**: `SOUL.md`, `MEMORY.md` and `USER.md` are always in the system prompt; skills are
  indexed. See [dream mode](dream.md#per-agent-files). A new agent's `SOUL.md` starts as a short default text that explains
  these files and the tools that edit them; the agent owns it and may rewrite it. To reset or opt in, empty the SOUL editor
  and use **Insert default template** in **Soul & memory**.
- **associative memory**: before each reply RECALL searches the agent's index and suggests the best fragments to the agent
  (modes, settings and the per-user maximum are below).
- **dream mode**: background sessions in which the agent reviews recent conversations and curates its own files: [dream mode](dream.md).
- **user folders**: folders on the server (for example an Obsidian vault) that an agent may read and, optionally, edit
  (below). The Obsidian safe/auto overlay is a planned design: [overlay](../architecture/obsidian-overlay.md).
- **recall mode** (the agent enters its palace with a trimmed context and returns only what it needs) is planned, not implemented: [backlog](../project/backlog.md).

## associative memory

Each agent chooses in `chat_config` how it searches its memory before replying:

```yaml
chat_config:
  associative_memory: llm          # off | llm | question_only
  associative_execution: parallel  # parallel | sequential
```

- `off`: no search, no indicator and no associative-memory note in the system prompt
- `llm` (default): a small model reads the recent conversation and proposes a short
  search query, or decides nothing needs recalling
- `question_only`: the user's message (at most 1,000 characters) is searched as is, with
  no model call. It is faster, but it searches every message and cannot resolve references
  to earlier turns; the similarity threshold filters irrelevant hits

`associative_execution` (default `parallel`) chooses when the search runs. `parallel`
starts it beside the model call and, if it finds something while the reply is being
written, pauses the reply to add it (see
[associative recall in parallel](../architecture/overview.md#associative-recall-in-parallel)).
`sequential` waits for the search to finish before the model starts, so its result is
always in the first call and a reply is never paused, at the cost of the search time
added to the first token. The system prompt only mentions mid-reply hints in
`parallel`. It has no effect when `associative_memory` is `off`.

`true` and `false` from older agent files are still accepted and mean `llm` and `off`.
Other values are rejected when the agent is loaded. A chat without memory
(`has_memory: false`) never searches.

The root `settings.yaml` holds the framework-wide tuning shared by all agents. The
`llm` mode query step is a small, fast model call that runs before every reply, so it
should not use the chat's own model:

```yaml
associative_memory:
  context_messages: 6
  top_n: 3
  score_threshold: 0.35
  query_model: openai/gpt-4o-mini
  api_key: OPENROUTER_API_KEY
  # zdr: true   # optional override of the user's require_zdr
```

`query_model` is an OpenRouter model ID (or a `claude_subscription:<model>`
reference, which runs with the requesting user's own Claude login; see
[claude subscription](claude-subscription.md)) and the optional `zdr` overrides the
user's `require_zdr` for these requests (true restricts routing to zero data
retention providers; unset follows the user). `api_key` names the env var holding the shared OpenRouter key.
Set `query_model: null` to propose the query with each chat's own provider, model and
ZDR flag instead (the only option for Ollama-only setups); `question_only` uses
neither. If the env var is unset or the query request fails, the error is logged, the
turn continues without associative memory and the indicator shows "skipped".
`context_messages` (0-100) is how many recent messages the query model sees,
`top_n` (1-20) the application default for the maximum fragments injected per turn, and
`score_threshold` (0-1, default 0.35) the minimum similarity. 0.35 was picked by measuring on a small
test vault: unrelated questions injected something in 9 of 16 cases at 0.25 but 3 of 16 at 0.35 with the same
recall, while 0.45 already dropped many correct hits; re-measure when the embeddings model changes. Each user can override the maximum in the web
Settings (`associative_max_matches` in their `preferences.json`, integer 1-20; empty or absent follows
`top_n`); a change applies from the next message. `score_threshold` is application-only and cannot be
set per user.
Each fragment is shown with its address and heading path (or chat title and date) and
up to 400 characters of its text, cut at a sentence end when possible. Chunks of the
chat being answered are never suggested: that chat is already in the context.

## memory index

The search runs over the agent's vector index (`data/<user_id>/<agent_id>/.index`),
which covers the mind palace, the agent's skills (`agent:skills/**`), its conversations
and every user folder with `indexed: true` (see [user folders](#user-folders)).
`SOUL.md`, `MEMORY.md` and `USER.md` are not indexed: they are in every system prompt.
Hidden entries (names starting with `.`) are skipped. The search never updates the
index; it queries what is already indexed, which is kept current in the background:

- after each reply: the chat's new exchanges and the files the turn's tools wrote;
- when a chat opens and when a dream starts: a stat-only reconcile of the agent's own
  sources (unchanged files are never read; deleted files and chats are dropped);
- user folders: when a chat opens or the index worker is idle, once
  `folders_reconcile_minutes` have passed since the last scan, and on demand with
  **Reindex** in the agent's Folders tab.

The framework-wide settings live in the `index` block of the root `settings.yaml`:

```yaml
index:
  dir_name: .index          # index folder inside each agent's data folder
  collection: memory        # Chroma collection name
  file_types: [txt, md, jsonl]  # files the text search tools read; md and txt are indexed
  chunk_tokens: 400         # target chunk size, in estimated tokens (characters / 4)
  max_chunk_tokens: 600     # larger paragraphs and code blocks are split
  overlap_tokens: 60        # repeated at the start of the next chunk of the same section
  embed_batch: 64           # texts per embeddings request
  folders_reconcile_minutes: 10
```

Markdown is split by headings, then packed by paragraphs; fenced code blocks are kept
whole unless they exceed `max_chunk_tokens`. Each chunk starts with its context
(`<source>:<path> > <title> > <heading path>`). A conversation chunk is one exchange
(the user message and the reply), prefixed with the chat title and date. Skills are
one chunk per section, prefixed with the skill name and description.

The collection stores the embeddings model and the chunking scheme (a version plus
the three sizes above). When either differs from the current configuration (for
example after changing `models.embeddings` or a chunk size), the index is dropped and
rebuilt in the background on the next chat or dream start; searches return only what
the new index already holds until it is complete. The web UI shows the index status
and can force a rebuild (see [managing agents](agents.md#managing-agents)).

The web and Telegram processes may share a data folder. Chroma's persistent client is
not safe across processes, so every index access holds a short cross-process lock
(`.index/store.lock`) and reloads Chroma when another process wrote since; embedding
work holds a second lock (`.index/write.lock`) per file or chat. See the module notes
in `src/core/index.py`.

## user folders

`folders` lists folders on the machine running RECALL (an Obsidian vault, a project
notes folder) that one agent may use. each folder becomes a source named after it, so
`notes:Projects/plan.md` addresses a file inside the `notes` folder, with the usual path
guard; hidden entries (`.obsidian/`, `.git/`, `.trash/`) are left out of listings,
searches, reads and the index.

- names match `^[a-z0-9_-]{1,32}$`, are unique within the agent and cannot be a built-in
  source (`mind_palace`, `conversations`, `agent`, `dream`); invalid names are a `422`.
- the path must be an existing folder: saving an agent with a missing one is a `400`
  naming the folder. When an agent loads and a folder is missing (an unmounted drive, a
  moved vault), that folder is skipped with a warning in the log and the agent works
  without it; its indexed chunks are kept.
- `indexed: true` (default) adds the folder to the [memory index](#memory-index).
- in chats, `folders_read` gives `folder_list`, `folder_read` and `folder_search` over every
  folder, and `folders_edit` adds `folder_write`, `folder_append`, `folder_edit`,
  `folder_move` and `folder_delete`; with either group the generic memory tools can also
  read the folders, and with `folders_edit` they can write them. `read_only: true` refuses
  every write to that folder, whatever the groups. without a folder group the folders are
  still indexed but no tool can read them.
- dreams only read folders, and only with the `folders` dream tool group.

the former single `vault: <path>` key was removed; an agent file that still has it is
rejected with a message pointing to `folders`. move the path to
`folders: [{name: vault, path: <path>}]` and rename the `vault_read` / `vault_edit` tool
groups to `folders_read` / `folders_edit` and the `vault` dream tool group to `folders`.

## Editing an earlier message

Hover over (or, on touch screens, look at) one of your own messages in the web UI and
press the pencil to edit it. The bubble turns into a text box with *Save and resend*
(Ctrl/Cmd+Enter) and *Cancel* (Esc), and says how many messages after it will be removed.
Saving rewinds the same chat to that point: the message and everything after it (replies,
tool calls, later messages) leave the chat and the agent answers the new text from the
earlier history. There is no fork; the chat keeps its id.

- Attachments of the original message are kept and shown read-only; to change them, send a
  new message instead. Edit buttons are disabled while a reply is streaming.
- The removed turns are not deleted: they are appended to `<chat_id>.removed.json` next to
  the chat file (a list of `{removed_at, turns}`), which is also deleted with the chat.
- A generated chat title is dropped when you edit the first message (a title you chose
  stays), and the associative-memory context of the chat is rebuilt from the kept turns.
- Long-term memory is **not** rewound: memories the agent already wrote, Dream sessions and
  SOUL changes made from the removed turns stay as they are, and an index of the chat is
  refreshed with the next reconcile.
- Only your own stored messages can be edited; `/dream` command replies are not stored and
  have no edit button. Telegram has no edit control.
