# user.yaml and other per-user files

Everything RECALL stores for one user lives in `.config/<user_id>/` (ignored by Git; in Docker it is the `config/` mount).
Agent files are described in [agent.yaml](agent-yaml.md); framework-wide values are in [settings.yaml](settings-yaml.md).

| file | purpose |
| --- | --- |
| `user.yaml` | identity, web password hash, language, ZDR policy, main provider and its key variable name, model defaults, provider options, agent ids |
| `agents/<agent_id>.yaml` | one file per agent, see [agent.yaml](agent-yaml.md) |
| `preferences.json` | interface preferences shared by web and Telegram (see below) |
| `openrouter-favorites.json` | favorite models, see [models and providers](../user-guide/models-and-providers.md#favorite-models) |
| `mcp/<server_id>/` | the user's MCP credentials, see [MCP servers](../user-guide/mcp.md) |
| `agents.lock`, `preferences.lock` | coordination files for atomic writes; never delete them between requests |

Keys of `user.yaml`: `id`, `name`, `password_hash` (optional), `language`, `require_zdr`, `max_level`, `provider`, `embeddings_provider`
(optional, falls back to `provider`), `agents`, `models`, `openrouter`, `ollama`, `claude_subscription`, `terminal_backends` and `approval_rules`. Unknown keys inside
the provider blocks are rejected.

## max_level (permission levels)

`max_level` is the highest [permission level](../architecture/permissions.md) the user may give their agents: `minimal`, `reader`,
`assistant`, `collaborator`, `operator` or `admin` (lowercase in the file; any case is accepted as input). **Only an administrator
sets it**, with the [admin CLI](../user-guide/admin-cli.md) (`user set --user U --set max_level=operator`, `create-user --max-level`).
The web API and the Settings dialog never read or write it, so a user cannot raise their own ceiling.

`max_level` is **required**: a `user.yaml` without it does not load, and the error names the fix,
`python -m src.admin user set --user U --set max_level=<level>`. An agent whose level (declared, or what its configuration needs) is
above this value is invalid (not loaded, nor saved by the API or the CLI); `user set` refuses a `max_level` below the level of one of
the user's agents. The level is enforced when tools are built and at dispatch, in chats, workers and dreams (see
[permissions](../architecture/permissions.md)).

## storage layout

yaml, editable by hand, one file per entity, nested per user. one file per agent. the folder is `.config/` (kept distinct from the `src/config` code package).

```
.config/
  <user_id>/
    user.yaml                # user: provider, agents (channels/resources planned)
    agents/
      <agent_id>.yaml        # one file per agent
```

agents can be created, edited, renamed and deleted from the web **Agents** tab or the `/api/agents` routes (see [managing agents](../user-guide/agents.md#managing-agents)); hand-written files work too. the `id` field inside the file identifies the agent, so the file name is only a convention. isolation is natural per user folder. everything except secrets is versionable in git. a global `defaults.yaml` with merge is planned but not yet used.

## provider & secrets

each **user** picks a provider by name and (for cloud providers) brings their own key, referenced by env var name and resolved from the environment / `.env`. local providers (ollama) need no key:

```yaml
# user.yaml
language: en                         # language for user-facing capability errors
require_zdr: false                   # optional, off by default: only use zero-data-retention routes (see below)
provider:
  name: openrouter                    # selects the provider implementation
  api_key: OPENROUTER_ALICE_API_KEY   # env var NAME holding the key; omit for local providers
```

API keys never live in YAML. Providers and model IDs belong to the user;
agents configure behavior, not inference or embedding models.

`provider` is the user's **main provider**: it serves bare model ids and
embeddings. It is set in `user.yaml` only, not in the web UI. The other providers
need no setup in the user's file: the model picker offers every provider the
server can use (see [providers](../user-guide/models-and-providers.md#providers)).

## model references

A model reference is a bare model id, served by the user's main provider, or
`<provider>:<model>`, served by another provider: `openrouter:vendor/model`,
`ollama:llama3:8b`, `claude_subscription:sonnet`. Only the provider names
`openrouter`, `ollama` and `claude_subscription` count as a prefix, so ids with
their own colon (`llama3:8b`, `vendor/model:free`) stay bare ids. An explicit
prefix for the main provider is accepted and saved in its bare form. The same
reference works everywhere a model is chosen: `models.*` in `user.yaml`, the
default and per-chat pickers, favorites, an agent's `dream.model` and Telegram.

A bare id always means the **current** main provider; changing the main provider
in `user.yaml` does not rewrite stored references (there is no migration step).
The web model picker chooses the provider on the picker itself and saves the
reference in this form: bare for the main provider's models, prefixed for the
others.

Capabilities: OpenRouter serves chat, speech-to-text and embeddings; Ollama chat
and embeddings; the experimental Claude subscription chat only (see
[claude subscription](../user-guide/claude-subscription.md) for setup, privacy flags and the
terms caveat). `models.stt` and `models.embeddings` reject a prefix whose provider
lacks that capability, and the main provider must be `openrouter` or `ollama`.

## optional provider settings

Optional provider settings stay in `user.yaml`, with no web UI. All blocks are
optional:

```yaml
provider:                 # main provider: serves bare model ids and embeddings
  name: openrouter
  api_key: OPENROUTER_API_KEY     # env var NAME of the main provider's key
openrouter:               # key variable of OpenRouter when it is not the main provider
  api_key: OPENROUTER_API_KEY     # env var NAME
ollama:
  host: null                      # null = OLLAMA_HOST, else http://localhost:11434 (also for a main Ollama)
claude_subscription:      # advanced options only, see claude-subscription.md
  claude_config_dir: null
  command: null           # bare executable name only (no path separators); absolute paths are admin-only
  claude_code_telemetry: false
  timeout_seconds: 300
```

Who may use the CLI's default login (no `claude_config_dir`) is a server-wide
setting, `claude_subscription.default_login_users: [<user_id>]` in `settings.yaml`
(empty by default = nobody; see [claude subscription](../user-guide/claude-subscription.md)).
`claude_config_dir` must resolve inside `<config dir>/<user_id>/`; `command` must be a bare executable name. An administrator
can set an absolute `claude_subscription.command` in `settings.yaml` (or `RECALL_CLAUDE_COMMAND`).

The blocks have no `enabled` key (the strict schema rejects it). A model whose
provider is not available fails with `provider not available: <provider>: <what to
fix>`: choosing it in a picker or the Dream tab is refused with the same message in
the user's language, and a reply, dream or chat title that needs it later reports
it (a missing OpenRouter key variable, or Claude Code's own "not installed" /
"run `claude auth login`" error). RECALL never logs in to Claude from the web and
never reads credential files; the status check only learns whether the CLI is
logged in (the account e-mail is never passed on).

## language

`language` is an optional user preference used for messages generated by the
channel and service when a selected model lacks an input modality. It accepts a
two-letter language code (`en`, `es`) or a language-region code (`en-US`,
`es-ES`) and defaults to `en`.

## zero data retention is a user policy

`require_zdr` (boolean, default `false`) in `user.yaml` is the one switch for
[zero data retention](../user-guide/models-and-providers.md#openrouter-model-catalog-and-zero-data-retention). It is the
default of every ZDR-aware feature: new chats, the model picker's checkbox when it
opens, default-model validation, Telegram's lists, chat naming, the associative-memory
query model, dream and speech-to-text. RECALL does not force or label ZDR: the
privacy of an account is managed in the provider (for example OpenRouter's
account-wide ZDR setting, or Anthropic's training opt-out), and a user who does not
care simply uses any model. A user who wants the guarantee sets `require_zdr: true`.

The per-feature `zdr` keys (`chat_naming.zdr`, `associative_memory.zdr`,
`speech_to_text.zdr` in `settings.yaml`) remain valid as overrides; when unset they
inherit `require_zdr`. A chat's stored `zdr` flag overrides it too (see
[choosing a model per chat](../user-guide/models-and-providers.md#choosing-a-model-per-chat)). The decision lives in
`src/config/zdr.py`.

## shared user preferences

User-wide channel preferences are stored separately from YAML in the ignored
`.config/<user_id>/preferences.json`. Telegram `/settings` sends one message
with inline buttons. Choices do not post messages to the chat: each button
sends a callback, the bot saves the change, answers the callback and edits the
same message in place. It offers thinking visibility, English/Spanish alerts,
the active agent, default LLM/STT models and **Close**, which removes the
buttons. Agent and default-model choices open paginated lists in the same panel
(LLM favorites first) with **Back**; a choice saves immediately and returns to
the main view, without requiring a chat. Opening `/settings` again removes the
previous panel's buttons; pressing a button on an old panel shows an expiry
alert. Opening any inline panel briefly sends and deletes a cleanup message
with `ReplyKeyboardRemove`, clearing reply keyboards left by older bot
versions before the inline panel is displayed. The web
sidebar's **Settings** dialog edits thinking, language and default LLM/STT
models (the audio model is set only here); agent switching is the **Agent**
selector in the chat header. The web sidebar lists the chats of all of the
user's agents (`GET /api/chats`), each labelled in italics with its agent.
Opening a chat from another agent makes that agent active; choosing an agent in
the selector starts a new chat with it. The per-chat reasoning effort is also available in Telegram's
`/settings` panel (**Chat reasoning effort**).
Thinking visibility, last-used and default agent selection and English/Spanish operational
notices/errors are implemented in both channels.

```json
{"show_thinking": false, "language": "es", "agent_id": "aria"}
```

- `show_thinking`: a display preference, defaulting to `true`. It does not
  disable model reasoning or delete saved reasoning.
- `language`: `en` or `es` for RECALL-generated alerts/errors, not model replies
  or third-party error text. Without an override, Spanish user YAML language
  codes resolve to `es`; other codes resolve to `en`.
- `disposable_retention_days`: integer 1-365, days of inactivity after which a
  [disposable chat](../user-guide/memory.md#disposable-chats) is deleted. Absent or
  `null` means 30; `GET /api/preferences` returns `null` until it is set. Edited in the
  web **Settings** dialog.
- `orchestrator`: `true`, `false` or `null`. `null` (default) follows `orchestrator.enabled` in
  `settings.yaml`; the response also carries the read-only `orchestrator_default`. Turns the
  [Telegram orchestrator](../user-guide/telegram.md#telegram-orchestrator-automatic-agent-choice) on or off for this
  user. Non-boolean values are `422`.
- `default_agent_id`: an agent id this user owns, or `null` (default: the user's first agent). The agent a new chat
  opens with when none is picked (web login without a chat to reopen, Telegram idle-timeout chats and single-agent
  `/newchat`) and the orchestrator's fallback when it is unsure and no core agent is set. `agent_id` stays the last
  used agent. An unknown or foreign agent is `404`; deleting the agent clears it and renaming follows the new id.
- `core_agent_id`: an agent id this user owns, or `null` (default: the default agent). The orchestrator's fallback
  agent when it is unsure (greetings, general talk). An unknown or foreign agent is `404`; deleting the agent clears
  it and renaming follows the new id.
- `avatar_style`: `circle` (default) or `free`; how the user's own profile picture is drawn: cropped into the accent-bordered circle, or
  shown as it is with no circle or border (for transparent PNGs). Anything else is `422` and it cannot be `null`. Edited in **Settings >
  General > Profile picture** (applied at once); the picture itself is uploaded with `PUT /api/me/avatar`
  ([API](api.md#profile-pictures)) and stored as `data/<user_id>/.avatars/_user.png`.
- `agent_id`: must identify an agent configured for this user with an existing
  agent configuration. Without an override, or with `null`, it resolves to the
  user's first agent; users without agents receive `null`. This preference is
  used for new chats, not moving existing chats or interrupting replies.

The web refreshes preferences at login, when opening settings or a chat, before
sending or creating a chat, and when its window regains focus. Hiding thinking
applies to both existing history and live reply blocks; the separate local
expanded/collapsed preference is unchanged. The current conversation stays on
its own agent until a new chat is requested or the user explicitly switches
agents. Selecting an agent in the web chat header also saves the shared preference.

Telegram reads the shared agent when creating a chat, including automatic
first-message/idle-timeout chats. Thinking visibility is read at the beginning
of each reply and applies to previews, final messages and interrupted replies.
Tool-call lines stay visible even when their preceding thinking is hidden.
Previously delivered Telegram messages are not edited retroactively.

Telegram reloads the alert language before each incoming update; choosing a
language in `/settings` translates that settings menu immediately. The web
localizes its own error/notification templates, settings controls and retention
warning after refreshing preferences. HTTP and SSE application errors use the
saved language; opaque provider errors and low-level technical diagnostics stay
unchanged. The public `GET /api/localization?language=en|es` catalog keeps Python
and browser templates consistent. Template values such as filenames, agent IDs
and provider error text are never translated.

Cached service agents receive the current user language before each generation,
so unsupported-modality errors also follow preference changes without restarting
or rebuilding the agent. If preferences cannot be read, the failure is reported
explicitly: Telegram uses its last successfully read language, while HTTP errors
use the browser's last-known `Accept-Language` header and log the lookup failure.

## preferences API

`GET /api/preferences` returns resolved fields for the current
cookie user. `PATCH /api/preferences` accepts any subset of the fields and
returns the resolved result. Omitted fields keep their saved overrides, or
continue following configuration defaults when never explicitly saved.
For example, `{"show_thinking": false}` leaves language and agent unchanged.
Unknown fields, unsupported languages, non-boolean thinking values and malformed
agent IDs are rejected; an agent belonging to another user is never accepted.

Preference reads are not cached, so separate web and Telegram processes can
observe each other's changes on their next read without restarting. Updates
merge under an operating-system file lock and use atomic replacement; the
adjacent `preferences.lock` file is persistent coordination state, not a stale
lock to delete between requests. Invalid files, failed writes and saved agents
that are no longer available produce explicit errors rather than silently
resetting preferences. A removed agent can be replaced with a valid selection
or reset with `{"agent_id": null}`. Neither user YAML nor model favorites are
rewritten.

`GET /api/preferences` also returns two read-only objects. `limits` holds the size limits of `SOUL.md`, `MEMORY.md` and `USER.md`. `defaults`
holds what each setting follows when nothing is saved, for the Settings dialog's *Default: X* hints and reset buttons:

| Key | Value |
| --- | --- |
| `associative_max_matches` | `{default, min, max}`: `associative_memory.top_n` from `settings.yaml`, bounds 1-20 |
| `disposable_retention_days` | `{default, min, max}`: 30, bounds 1-365 |
| `orchestrator` | `orchestrator.enabled` from `settings.yaml` |
| `show_thinking`, `theme` | `true`, `"system"` (browser only) |
| `default_agent_id` | `null` (the first agent) |
| `core_agent_id` | `null` (the default agent) |
| `avatar_style` | `"circle"` |
| `language` | the user's `language` from `user.yaml` reduced to `en` or `es` |
| `default_llm_model`, `default_stt_model` | `models.chat` and `models.stt` of `user.yaml` |
| `agents` | `[{id, name}]` of the user's agents, for the core-agent choice |

Resetting a setting means saving `null` for it (`associative_max_matches`, `disposable_retention_days`, `orchestrator`,
`default_agent_id`, `core_agent_id`, `default_llm_model`, `default_stt_model`): the value is stored as absent and keeps following the default. `show_thinking`
and `language` always hold a value, so their reset saves the default value.

## creating users (administrator)

Instead of hand-editing, run inside the container (or `python -m src.admin ...` on a plain install):

```bash
docker compose exec -it recall-web python -m src.admin create-user <user_id> [--name N] [--language en] \
    [--api-key-env VAR] [--claude-login] [--generate] [--max-level LEVEL]
docker compose exec -it recall-web python -m src.admin set-password <user_id> [--generate]
```

`create-user` writes a valid `user.yaml` with the default `models`, `agents: []`, `max_level` (`--max-level`; default `collaborator`), an Argon2 `password_hash` and
`provider.api_key` set to the variable NAME (`OPENROUTER_API_KEY` by default; the key is never asked for or
stored). `--claude-login` sets `claude_subscription.claude_config_dir` to `<config dir>/<user_id>/claude-login`.
The password is typed twice (hidden) or generated (`--generate`, 24 characters, printed once); passwords shorter
than 8 characters or trivially weak are refused, and an existing user is never overwritten. `set-password` rewrites
only `password_hash`; YAML comments in the file are lost. See [deployment](../deployment/docker.md#users-and-passwords).

Other admin commands (see [Admin CLI](../user-guide/admin-cli.md)): `python -m src.admin user show --user <id>` prints this file
with key variable NAMES and never the password hash; `python -m src.admin user set --user <id> --set path=value` changes non-secret
fields (`name`, `language`, `models.*`, `provider.name`, `provider.api_key` = a variable NAME, `terminal_backends.*`,
`claude_subscription.claude_config_dir`...) after validating the whole file with the user model (the hash, `id` and `agents` are
refused; comments are lost). Store the key itself with `python -m src.admin set-secret NAME`, which writes
`.config/secrets.env` (a fallback for variables that are not in the process environment, re-read when it changes).

## terminal backends

`terminal_backends` maps a name (`^[a-z][a-z0-9_-]{0,31}$`) to a machine an agent's `terminal.backends` can use (a sandbox, your own PC...). Unknown keys
are rejected; key files are paths, never inline secrets.

| type | keys |
| --- | --- |
| `ssh` | `host` (name or address), `port` (default 22), `user`, `key_file`, `known_hosts_file` (all required except `port`), `description` and `kind` (optional); strict host-key checking and key-only login are always on |
| `local` | `workdir`, `description` (optional); refused unless `settings.yaml` sets `terminal.allow_local: true` or the server sets `RECALL_TERMINAL_ALLOW_LOCAL=true` |

Every backend also accepts an optional `description` (free text, at most 200 characters, for example `User A's Windows PC, real
files, Git Bash`). The model sees it next to the backend's name in its instructions, and the agent form shows it; use it to say
whether the machine is a disposable sandbox or a real machine.

`kind` (ssh only) is `machine` (default: one of your real machines) or `sandbox` (a disposable one). Approval rules can match it with `backend_kind`, and allowing everything is allowed on a `sandbox` from the `operator` level but needs `admin` on a `machine`. A `local` backend always has the kind `local` and takes no `kind` key.

Check a backend with `python -m src.admin terminal-test --user <user_id> --backend <name>` (runs `echo ok; uname -sr`, 15 s
timeout, prints the exit code and the redacted output, never the key). Setup walk-through: [terminal guide](../user-guide/terminal.md).

## approval rules

`approval_rules` (optional) is the user layer of the [approval rules](../user-guide/terminal.md#approval-rules), applied to all the user's agents on top of the
installation list ([settings.yaml](settings-yaml.md#approval_rules)) and under each agent's own list: `default` (`allow`, `ask` or `deny`) and `rules`
(`{verdict, tool, id, command, path, backend, backend_kind, dangerous, reason}`). Unknown keys are rejected. Deny beats ask beats allow across the three layers.
A rule or default that allows everything on a `machine` or `local` backend is only valid for admin agents.

## example

```yaml
# .config/<user_id>/user.yaml
id: user-a
name: User A
password_hash: "$argon2id$..."          # web login; create it with `python -m src.admin set-password <user_id>`
language: en
require_zdr: false                      # optional; true requires zero-data-retention routes everywhere
max_level: collaborator                 # highest permission level of this user's agents; set only by an administrator
provider:
  name: openrouter                      # main provider: openrouter or ollama
  api_key: OPENROUTER_USER_A_API_KEY   # env var NAME holding the key; omit for ollama
agents: [aria]
models:
  chat: openai/gpt-4o-mini              # or ollama:llama3:8b / claude_subscription:sonnet
  stt: openai/whisper-large-v3-turbo
  embeddings: openai/text-embedding-3-small
# claude_subscription:                  # optional, advanced; see the Claude subscription guide
#   claude_config_dir: /path/to/this-users-claude-login
# terminal_backends:                    # optional; sandboxes the user's agents may run commands in (see below)
#   sandbox: {type: ssh, kind: sandbox, host: 192.0.2.10, port: 22, user: agent, key_file: /app/.config/keys/sandbox_ed25519, known_hosts_file: /app/.config/keys/sandbox_known_hosts, description: Disposable sandbox}
# approval_rules:                       # optional; rules for all of this user's agents (deny beats ask beats allow)
#   rules: [{verdict: allow, tool: terminal, backend_kind: sandbox, command: "git status"}]
```

A complete, working example lives in `.config/example_user/`.
