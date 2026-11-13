# Configuration fields

Unknown keys are rejected in the agent yaml and most blocks (not in `chat_config`). The user edits these or uses the Agents tab; an agent with the `agents` group uses its tools (`references/permissions.md`), an admin the CLI (`references/admin-cli.md`). Keys are dotted.

## When a change takes effect

| Change | Applies |
| --- | --- |
| Agent yaml saved from the Agents tab | next message (cached runtime is rebuilt); refused with 409 while a reply is active or queued |
| Agent yaml edited by hand or with `python -m src.admin agent set` | next message (rebuilt between replies); an INVALID file keeps the last good config (error logged, notice in the chat) |
| `user.yaml` (`user set`, by hand) | same: next message |
| `settings.yaml`, `.env`, `RECALL_*` variables | restart web and Telegram |
| a key stored with `set-secret` | next message (`references/admin-cli.md`) |
| SOUL, memories, skills (yours or the user's edits) | new chats; a running chat keeps its prompt |
| `chat_config.recall_knowledge` | prompt is built per chat: new chats |

## Agent yaml (`.config/<user_id>/agents/<agent_id>.yaml`)

| Field | Meaning (default) |
| --- | --- |
| `id` | `[A-Za-z0-9_-]{1,64}`, names the data folder |
| `name` | 1-100 chars; the DISPLAY NAME in the web UI and Telegram, separate from the SOUL: when renaming update both (`soul_edit`, `agent set --set name="New"`). The `id` normally stays |
| `description` | 0-500 chars; shown in the agent list, read by the Telegram orchestrator |
| `chat_config` | block below |
| `dream` | block below |
| `folders` | up to 20 user folders |
| `terminal` | block below; needed by the `terminal` tool group |
| `delegation` | block below; the `delegate` tool group, which gets the defaults when this is omitted |
| `approval_rules` | agent layer of approval rules `{default, rules}`; cards append exact `allow` rules (`references/terminal-and-approvals.md`) |
| `level` | `minimal`, `reader`, `assistant`, `collaborator`, `operator` or `admin`; between what its tools need and the owner's `max_level`, else invalid. Unset = what its tools need |
| `avatar_style` | `circle` (default) or `free` (the profile picture shown as it is, no circle). The user uploads the picture in the web form; agents never touch it |

### `chat_config` (Agents tab: Basic, Basic > Advanced and Tools)

Basic holds Identity and Model and reasoning (`model`, `reasoning_effort`, `reasoning`, `keep_reasoning_in_memory`, `reasoning_tokens`); Advanced holds `stream`, `has_memory`, `recall_knowledge`, `associative_memory`, `associative_execution`, `max_tool_hops` and `compaction`. Rows show their default and a reset button.

| Field | Meaning (default) |
| --- | --- |
| `stream` | stream tokens (true) |
| `has_memory` | keep conversation history in context (true); false also disables associative search |
| `reasoning` | request reasoning (true) |
| `reasoning_effort` | minimal, low, medium, high, xhigh, max (medium) |
| `reasoning_tokens` | optional reasoning budget |
| `keep_reasoning_in_memory` | store thinking in history (true) |
| `max_tool_hops` | tool rounds per turn, 1-100; empty = settings value (50) |
| `compaction` | context compaction of long chats, see the table below |
| `tools` | allow tools at all (true) |
| `tool_groups` | list of groups; omitted means `memory_read`, `memory_edit`, `web`, `self` |
| `allow`, `deny` | extra per-tool filters |
| `mcp` | MCP servers enabled for this agent, keyed by server id |
| `associative_memory` | `off`, `llm` (default), `question_only` |
| `associative_execution` | `parallel` (default) or `sequential` |
| `model` | default chat model, `provider:model` or a bare id; empty follows the user's |
| `recall_knowledge` | gives you this built-in skill (true) |

`chat_config.compaction`: when the context passes `trigger`, a small model writes my own notes (first person) on the older part, merged with the previous notes; they replace it as a system message right after the system prompt, which is never touched. The recent tail is kept verbatim and always opens on a user message. The full history stays in the chat: I look details up with `session_search`. Runs after a reply is delivered (or before a turn that finds it due); `/compact [instructions]` (web chat and Telegram) runs it by hand, answered by RECALL itself; the web shows each compaction as a divider with the tokens before and after. If the summary model fails, the oldest messages are cut once the context reaches 90 % of the window (`[CUTOFF CONVERSATION]`).

| Field | Meaning (default) |
| --- | --- |
| `compaction.enabled` | compact long chats (true) |
| `compaction.mode` | unit of `trigger` and `keep_recent`: `percent` of the model's context window (default) or `tokens` |
| `compaction.trigger` | compact when the context reaches this (50 %; tokens mode: at least 1000) |
| `compaction.keep_recent` | recent tail kept verbatim (20 %), smaller than `trigger` |
| `compaction.model` | summary model, `provider:model` (`claude_subscription:haiku`); empty uses the chat's own model; ZDR never applies to a Claude subscription model |

MCP entry fields: `tools` (list; omitted means all of the server's tools), `read_only`, `instructions`.

### `dream` (Dream tab)

| Field | Meaning (default) |
| --- | --- |
| `dream.enabled` | run the schedule (false); manual sessions work anyway |
| `dream.every_hours` | every N hours (1-720); not with `daily_at` |
| `dream.daily_at` | local `HH:MM` once a day |
| `dream.model` | empty uses the chat model the agent resolves to |
| `dream.reasoning_effort` | medium |
| `dream.max_steps` | model calls per session (60) |
| `dream.max_tokens` | in + out per session (500000) |
| `dream.max_minutes` | wall-clock limit, 1-120 (5) |
| `dream.tools` | extra read groups: `folders` (default; needs a folder read group), `web` (opt-in) |
| `dream.prompt` | empty uses the built-in prompt |
| `dream.notify_telegram` | notify when done (false) |

### `folders` entries

| Field | Meaning (default) |
| --- | --- |
| `name` | `^[a-z0-9_-]{1,32}$`, unique; not `mind_palace`, `conversations`, `agent`, `dream` |
| `path` | must exist when saving; system, config, data, logs, program, SSH folders are refused; secrets inside stay hidden |
| `indexed` | feeds associative memory and `query_memory` (true); removing or un-indexing a folder purges its chunks |
| `read_only` | never writable (false) |

### `terminal` (Tools tab)

| Field | Meaning (default) |
| --- | --- |
| `terminal.backends` | map of backend name (a key of the user's `terminal_backends`) to its policy |
| `workdir` | per backend, directory inside it (`~`) |
| `approvals` | per backend shorthand: `manual` (default), `smart`, `off` (quote it: bare `off` is a YAML boolean); `off` on a real machine needs level `admin` |
| `timeout_seconds` | agent-wide command timeout 1-600 (60); a backend may override it |
| `terminal.default_backend` | required with more than one backend |
| `terminal.max_output_chars` | 500-200000 (20000) |

### `delegation` (group `delegate`; see `references/delegation.md`)

| Field | Meaning (default) |
| --- | --- |
| `delegation.enabled` | false switches `task_start` off (true) |
| `delegation.allowed_groups` | groups a task may be given: only `web` (web) |
| `delegation.max_parallel` | running tasks per chat, 1-10; can only lower `settings.yaml` (5) |
| `delegation.timeout_seconds` | per task, 10-3600; can only lower it (600) |
| `delegation.max_tokens` | in + out per task; only lowers it (default: no cap) |
| `delegation.default_model` | model of fresh workers (the chat's) |
| `delegation.targets` | other agents it may hand tasks to: `{agent, remember}`; see `references/delegation-targets.md` |

## User yaml (`.config/<user_id>/user.yaml`; set by the admin, mostly not in the UI)

| Field | Meaning |
| --- | --- |
| `id`, `name` | identity |
| `password_hash` | web login (Argon2); set with `python -m src.admin set-password` |
| `language` | two-letter code such as `en` or `es` (optionally `en-US`), default `en` |
| `require_zdr` | zero data retention policy (false) |
| `provider` | main provider: `name` (`openrouter` or `ollama`) and `api_key` (NAME of the env var) |
| `embeddings_provider` | optional; falls back to `provider` |
| `agents` | the user's agent ids |
| `models` | defaults `chat`, `stt`, `embeddings`; `stt_language` (optional spoken language for transcription, e.g. `es`; beats `speech_to_text.language`) |
| `openrouter`, `ollama`, `claude_subscription` | provider options |
| `terminal_backends` | named machines agents may use |
| `approval_rules` | user layer of approval rules (`{default, rules}`) |
| `max_level` | required; highest agent `level`; only an admin sets it (`user set`) |

Provider options: `ollama.host`; `claude_subscription` takes `claude_config_dir` (inside the user's own config folder), `command` (a bare executable name), `claude_code_telemetry`, `timeout_seconds`; `openrouter.api_key` is an env var NAME.

Backends (`terminal_backends.<name>`): `type` is `ssh` (`host`, `port`, `user`, `key_file`, `known_hosts_file`) or `local` (`workdir`; refused unless `terminal.allow_local` in `settings.yaml`); both take `description`; `ssh` also takes `kind` (`machine` default, or `sandbox` for a disposable one). `local` is its own kind.

Preferences (`preferences.json`, Settings dialog and Telegram `/settings`) are separate from the yaml and apply from the next read. The Settings dialog has General, Models, Chats & memory, Telegram and Connections; a row's reset button stores nothing (null), so the setting follows the default again.

Claude plan usage: the web chat shows a strip next to the message box (session and week, as colours and bars) and `/usage` (web chat and Telegram) answers the same numbers as text; both read the user's own Claude login and are hidden or explained when there is none.
