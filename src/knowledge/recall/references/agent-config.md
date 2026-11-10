# Configuration fields

Unknown keys are rejected in the agent yaml and most blocks (not in `chat_config`). The user edits these files or uses the Agents tab; only the setup guide (terminal group, local backend) uses `python -m src.admin` (`references/admin-cli.md`). Keys use dotted names below.

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
| `name` | 1-100 chars; the DISPLAY NAME shown in the web UI and Telegram. Separate from the SOUL: when renaming an agent update both (`soul_edit` and `agent set --set name="New"`). The `id` is the folder/URL and normally stays |
| `description` | 0-500 chars; shown in the agent list and read by the Telegram orchestrator |
| `chat_config` | block below |
| `dream` | block below |
| `folders` | up to 20 user folders |
| `terminal` | block below; needed by the `terminal` tool group |
| `delegation` | block below; the `delegate` tool group, which gets the defaults when this is omitted |
| `approval_rules` | agent layer of approval rules `{default, rules}`; cards append exact `allow` rules (`references/terminal-and-approvals.md`) |
| `level` | `minimal`, `reader`, `assistant`, `collaborator`, `operator` or `admin`; between what its tools need and the owner's `max_level`, else invalid (the error names the service). Unset = what its tools need. Enforced at build and call |

### `chat_config` (Agents tab: Basic, Basic > Advanced and Tools)

Basic holds Identity (name, description, personality) and Model and reasoning (`model`, `reasoning_effort`, `reasoning`, `keep_reasoning_in_memory`, `reasoning_tokens`); the collapsed Advanced section holds `stream`, `has_memory`, `recall_knowledge`, `associative_memory`, `associative_execution` and `conversation_messages_retained`. Rows show their default (from `GET /api/agents/defaults`) and a reset button.

| Field | Meaning (default) |
| --- | --- |
| `stream` | stream tokens (true) |
| `has_memory` | keep conversation history in context (true); false also disables associative search |
| `reasoning` | request reasoning (true) |
| `reasoning_effort` | minimal, low, medium, high, xhigh, max (medium) |
| `reasoning_tokens` | optional reasoning budget |
| `keep_reasoning_in_memory` | store thinking in history (false) |
| `conversation_messages_retained` | last N messages in context (30); empty keeps all |
| `tools` | allow tools at all (true) |
| `tool_groups` | list of groups; omitted means `memory_read`, `memory_edit`, `web`, `self` |
| `allow`, `deny` | extra per-tool filters |
| `mcp` | MCP servers enabled for this agent, keyed by server id (see `tools` / `read_only` / `instructions` below) |
| `associative_memory` | `off`, `llm` (default), `question_only` |
| `associative_execution` | `parallel` (default) or `sequential` |
| `model` | default chat model, `provider:model` or a bare id; empty follows the user's |
| `recall_knowledge` | gives you this built-in skill (true); Advanced section checkbox |

MCP entry fields: `tools` (list; omitted means all of the server's tools), `read_only`, `instructions`.

### `dream` (Dream tab)

| Field | Meaning (default) |
| --- | --- |
| `dream.enabled` | run the schedule (false); manual sessions work anyway |
| `dream.every_hours` | every N hours (1-720); not together with `daily_at` |
| `dream.daily_at` | local `HH:MM` once a day |
| `dream.model` | empty uses the chat model the agent resolves to |
| `dream.reasoning_effort` | medium |
| `dream.max_steps` | model calls per session (60) |
| `dream.max_tokens` | input + output per session (500000) |
| `dream.max_minutes` | wall-clock limit, 1-120 (5) |
| `dream.tools` | extra read groups for dreams: `folders` (default; needs a folder read group), `web` (opt-in) |
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
| `delegation.max_parallel` | running tasks per chat, 1-10; can only lower `settings.yaml` (3) |
| `delegation.timeout_seconds` | per task, 10-3600; can only lower it (600) |
| `delegation.max_tokens` | input + output per task; can only lower it (200000) |
| `delegation.targets` | other agents of the same user it may hand tasks to: `{agent, remember}`; see `references/delegation-targets.md` (empty) |

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
| `models` | defaults `chat`, `stt`, `embeddings` |
| `openrouter`, `ollama`, `claude_subscription` | provider options |
| `terminal_backends` | named machines agents may use |
| `approval_rules` | user layer of approval rules (`{default, rules}`) |
| `max_level` | required; highest agent `level`; only an admin sets it (`user set`) |

Provider options: `ollama.host`; `claude_subscription` takes `claude_config_dir` (inside the user's own config folder), `command` (a bare executable name), `claude_code_telemetry`, `timeout_seconds`; `openrouter.api_key` is an env var NAME.

Backends (`terminal_backends.<name>`): `type` is `ssh` (`host`, `port`, `user`, `key_file`, `known_hosts_file`) or `local` (`workdir`; refused unless `terminal.allow_local` in `settings.yaml`); both take `description`; `ssh` also takes `kind` (`machine` default, or `sandbox` for a disposable one). `local` is its own kind.

Preferences (`preferences.json`, Settings dialog and Telegram `/settings`) are separate from the yaml and apply from the next read. The Settings dialog has General, Models, Chats & memory, Telegram and Connections; a row's reset button stores nothing (null), so the setting follows the default again.
