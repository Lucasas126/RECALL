# HTTP API and error codes

The web UI is a thin client of these routes (served by `src/web/app.py`). Everything except login, `GET /api/version` and `GET /api/localization`
needs the signed session cookie and is scoped to the session user. Application errors are `{"detail": "<localized message>"}`
plus an optional `"code"` (see [error codes](#error-codes)).

## route index

| area | routes |
| --- | --- |
| session | `POST /api/login`, `POST /api/logout`, `GET /api/me` (the profile and agents, each agent and the user with `avatar` (see [profile pictures](#profile-pictures)), plus the user's effective `max_level` (read-only, [permission levels](../architecture/permissions.md)) and `terminal_backends`: `[{"name", "type", "description", "kind", "min_level"}]` sorted by name, the user's terminal backends for the agent form (`kind` is `sandbox`, `machine` or `local`, `min_level` the level the backend needs); never hosts, users or key paths), `GET /api/localization?language=en\|es`, `GET /api/version` (public: `{"version": "0.1.0", "build": null}`; `version` comes from `pyproject.toml`, `build` is the first 7 characters of `RECALL_BUILD` or `null`) |
| preferences | `GET`/`PATCH /api/preferences` ([details](user-yaml.md#preferences-api), including the Telegram orchestrator's `default_agent_id`, `orchestrator` / `core_agent_id` and the read-only `orchestrator_default`; the response also carries a read-only `limits` object `{soul_chars, memory_chars, user_chars}` from the `memory_limits` block of settings.yaml and a read-only `defaults` object with the effective defaults and number bounds), `GET /api/speech-to-text/models` |
| models | `GET /api/claude/usage` ([plan usage](#claude-plan-usage)), `GET /api/providers/status`, `GET /api/models/catalog`, `GET /api/models`, `GET`/`POST /api/models/favorites`, `DELETE /api/models/favorites/<model_id>` ([models and providers](../user-guide/models-and-providers.md)) |
| agents | `GET`/`POST /api/agents`, `GET`/`PUT`/`DELETE /api/agents/<agent_id>`, `GET`/`PUT`/`DELETE /api/agents/<agent_id>/avatar` ([profile pictures](#profile-pictures)), `GET /api/tool-groups`, `GET /api/agents/<agent_id>/index`, `POST /api/agents/<agent_id>/index/reindex` (table below) |
| MCP | `GET /api/mcp`, `GET /api/mcp/<server_id>/tools`, `POST /api/mcp/<server_id>/connect`, `DELETE /api/mcp/<server_id>/connection` ([MCP servers](../user-guide/mcp.md)) |
| chats | `GET /api/chats`, `GET`/`POST /api/agents/<agent_id>/chats`, `POST .../chats/<chat_id>/disposable`, `GET`/`DELETE .../chats/<chat_id>`, `GET .../chats/<chat_id>/models`, `PUT .../model`, `PUT .../reasoning-effort`, `PUT .../title`, `POST .../chats/<chat_id>/messages` (SSE, see [architecture](../architecture/overview.md#web-steering)), `GET .../chats/<chat_id>/stream` ([following a running reply](#following-a-running-reply)), `POST .../chats/<chat_id>/stop` ([stopping a reply](#stopping-a-reply)), `GET .../chats/<chat_id>/approvals`, `POST .../chats/<chat_id>/approvals/<approval_id>` ([terminal approvals](#terminal-approvals)) |
| approval rules | `GET`/`DELETE /api/agents/<agent_id>/approval-rules`, `DELETE .../approval-rules/<index>`, `POST .../approval-rules/reset`, `GET .../approval-rules/effective` ([learned approval rules](#learned-approval-rules)) |
| levels and administration | `GET /api/levels`, `GET /api/admin/users`, `PUT /api/admin/users/<user_id>/max-level` ([permission levels in the web UI](#permission-levels-in-the-web-ui)) |
| subagent tasks | `GET /api/tasks`, `GET /api/tasks/<task_id>`, `POST .../steer`, `POST .../stop`, `GET .../chat` ([table below](#subagent-tasks)) |
| dream and soul | `.../dream/settings\|status\|start\|cancel\|sessions`, `.../soul` (the response also carries `max_chars`, the application-wide SOUL limit, and `default`, the default SOUL.md template); `.../memories/<target>` returns its `limit` from settings.yaml, `.../soul/history`, `.../memories/<target>` ([dream mode](../user-guide/dream.md#web-api)) |

## providers and model catalog API

API (authenticated, scoped to the session user):

- `GET /api/providers/status?capability=chat|stt`: `{"main", "providers": [{"id",
  "label", "main", "available", "reason", "detail", "experimental",
  "zdr_applies", "capabilities"}]}`, the providers able to serve the capability.
  `reason` is `no_api_key`, `unreachable`, `cli_missing`, `logged_out` or
  `cli_unknown` when not available; `detail` is the variable name, host or hint,
  never a secret value.
- `GET /api/models/catalog?capability=chat|stt&provider=<id>&zdr=true&q=`:
  `{"models": [...]}`, the models of that one provider. An unavailable provider
  or one that cannot serve the capability is refused with 400 and the reason.
  Without `provider`, speech-to-text means OpenRouter and chat merges the
  available providers (Telegram). Each row has `provider`, `provider_label`,
  `main_provider` and `zdr_applies`.

## error codes

HTTP application errors are `{"detail": "<localized message>"}`; when the error has a
stable machine-readable code the body also has `"code"`, and the SSE `error` event of a
reply is `{"message", "code"}` instead of a plain string. The code is optional: errors
without one keep the plain shape, and Telegram ignores it. The web UI maps each code to
a title, a hint and, where an action exists, a button; unknown codes fall back to the
message. Errors render as a dismissable card (`role="alert"`) in the transcript, not as raw
`[error]` text.

| code | raised when | web action |
| --- | --- | --- |
| `invalid_image`, `image_too_large`, `unsupported_image_type` | a profile picture upload is not a usable PNG/JPEG/WEBP (400), is over 2 MiB (413) or has another `Content-Type` (415); see [profile pictures](#profile-pictures) | the message is shown under the upload button |
| `no_agents` | the user has no agents (HTTP 404) | Create an agent (opens the new-agent form); the chat shows an empty state instead |
| `unknown_agent`, `unknown_chat` | the agent/chat does not exist or is not the user's | `unknown_chat`: Start a new chat |
| `missing_api_key` | the provider's API key variable is not set on the server | Choose another model |
| `provider_unreachable` | the Ollama/provider server did not answer | Choose another model |
| `claude_not_installed` | Claude Code is not installed | Choose another model |
| `claude_no_own_login` | the user has no `claude_config_dir` and is not listed in `default_login_users` (checked in the picker and before every Claude request) | hint to ask the administrator + Choose another model (no button sets it up) |
| `claude_not_logged_in` | Claude Code is not logged in | hint with `claude auth login` (shown, never run) + Choose another model |
| `provider_unavailable` | Claude Code did not report its login state | Choose another model |
| `model_unavailable` | the model is not in the provider's catalog/installed | Choose another model |
| `rate_limited` | the upstream provider answered 429 | Choose another model |
| `agent_busy` | the agent or chat still has active/queued replies (also when converting a chat to or from disposable) | none (wait) |
| `mcp_not_connected` | an MCP tool needs an account the user has not connected | Open Settings |
| `level_exceeds_max` | an agent's `level` is above the user's `max_level` (HTTP 400, on `POST`/`PUT /api/agents`) | none (choose a lower level) |
| `level_below_required` | an agent's `level` is below what one of its services needs; the message names the service and the level (HTTP 400, same routes) | none (raise the level or remove the service) |
| `level_required_above_max` | an agent uses a service that needs more than the user's `max_level` (HTTP 400, same routes; also for an agent without a `level`) | none (remove the service or ask an administrator) |
| `approval_rule_not_allowed` | a rule, a `default: allow` or the shorthand `off` allows everything on a `machine` or `local` backend for an agent below `admin` (HTTP 400, `POST`/`PUT /api/agents`) | shown under the rule editor |
| `admin_required` | the caller's `max_level` is not `admin` (HTTP 403, `/api/admin/...`) | none |
| `max_level_self_change` | an administrator tried to change their own `max_level` (HTTP 400) | none (ask another administrator) |
| `max_level_below_agents` | the new `max_level` is below the level of some of the user's agents (HTTP 409; the body also has `agents`: `[{"id", "name", "level", "declared"}]`) | the Users section lists them |

New codes are set with `UserMessageError(..., code="...")` or a class-level `code`.

## agent routes

| method | route | result |
| --- | --- | --- |
| `GET` | `/api/tool-groups` | shared tool-group catalog available to the user's agents |
| `GET` | `/api/mcp` | shared MCP server catalog with the user's connection status |
| `GET` | `/api/mcp/<server_id>/tools` | tools of a server the user has connected (`409` when not connected) |
| `POST` | `/api/mcp/<server_id>/connect` | starts the server's sign-in for the user; returns the status and sign-in URL |
| `DELETE` | `/api/mcp/<server_id>/connection` | stops the user's server and deletes their stored credentials |
| `GET` | `/api/agents` | full configuration of each agent the user owns, including `chat_config.tool_groups` and `level`, its effective permission level: the declared one, else the lowest level covering its configuration |
| `GET` | `/api/agents/defaults` | defaults and bounds of the agent form fields, generated from the configuration models: `{chat_config, dream, terminal}`, each field `{default, min, max}` (`null` where the model has no default or bound), e.g. `dream.max_steps` `{default: 60, min: 1, max: 500}`, `chat_config.max_tool_hops` `{default: null, min: 1, max: 100}` |
| `POST` | `/api/agents` | creates `agents/<id>.yaml` and appends the id to `agents` in `user.yaml` (`201`) |
| `GET` | `/api/agents/<agent_id>` | one agent's configuration, with the effective `level` and `avatar` |
| `PUT` | `/api/agents/<agent_id>` | replaces the agent; the body `id` may differ, which renames it (its profile picture moves with it). A body without `level` keeps the stored level, and one without `avatar_style` keeps the stored style |
| `DELETE` | `/api/agents/<agent_id>` | removes the agent from `user.yaml` and deletes its file (`204`) |
| `GET` | `/api/agents/<agent_id>/index` | memory index status: `sources`, `files`, `chats`, `chunks`, `last_reconcile`, `folders_reconciled_at`, `rebuilding`, `busy`, `model`, `error` |
| `POST` | `/api/agents/<agent_id>/index/reindex` | queues a reconcile of every source, folders included (`202`); body `{"full": true}` drops the index and rebuilds it |

The rules these routes follow (ids, renames, deletion, locks, `409` while replies run) are in
[managing agents](../user-guide/agents.md#managing-agents).

**Permission levels.** `POST`/`PUT /api/agents` accept an optional top-level `level` (`minimal`, `reader`, `assistant`, `collaborator`,
`operator`, `admin`; any case, always returned lowercase). One above the user's `max_level` is refused with HTTP 400 and code
`level_exceeds_max`; one below what the configuration needs, or a configuration that needs more than `max_level`, with
`level_below_required` / `level_required_above_max` (the rule applies to agents without a `level` too); an unknown name is a `422`. A `PUT` without `level` keeps the stored one. Reads always return the effective
`level`. The user's `max_level` is only exposed by `POST /api/login` and `GET /api/me`; no route accepts or changes it (it is not an
agent field and `PATCH /api/preferences` rejects it with `422`): only an administrator sets it, with the admin CLI.

## Profile pictures

Users and agents can have a picture. Only the owner reaches it: the routes are scoped to the session user, and an agent id that is not the
user's is a `404` (`unknown_agent`) before anything is read. An agent never sees these routes (the `permissions` tool group does not touch them).

| method | route | result |
| --- | --- | --- |
| `PUT` | `/api/agents/<agent_id>/avatar` | stores the picture of the agent; `200` `{"url", "has_alpha", "version"}` |
| `GET` | `/api/agents/<agent_id>/avatar` | the stored PNG; `404` (`no_avatar`) when there is none |
| `DELETE` | `/api/agents/<agent_id>/avatar` | removes it (`204`, also when there was none) |
| `PUT` / `GET` / `DELETE` | `/api/me/avatar` | the same for the user's own picture |

`PUT` takes the **raw image bytes** as the body (no multipart) with `Content-Type: image/png`, `image/jpeg` or `image/webp`. The declared type is only
checked against that list: the bytes are decoded with Pillow and the format is decided by their content. Limits: 2 MiB of body and 4096 x 4096
pixels (checked from the header, before decoding). The stored file is a PNG of at most 256 px on its longest side, with the EXIF rotation applied,
the first frame only and no metadata. `url` is the route to `GET` (add `?v=<version>`), `has_alpha` is true when some pixel is transparent, and
`version` changes with every upload. `GET` answers `Content-Type: image/png`, `X-Content-Type-Options: nosniff`,
`Cache-Control: private, max-age=0, must-revalidate` and an `ETag`; a request with a matching `If-None-Match` gets `304`.

| status | code | when |
| --- | --- | --- |
| `400` | `invalid_image` | not a decodable PNG, JPEG or WEBP (SVG, GIF, text, truncated files) or more than 4096 pixels on a side |
| `413` | `image_too_large` | more than 2 MiB (refused while reading, without buffering the rest) |
| `415` | `unsupported_image_type` | the `Content-Type` is not `image/png`, `image/jpeg` or `image/webp` |
| `404` | `no_avatar`, `unknown_agent` | `GET` without a picture; an agent the user does not own |
| `400` | none | the agent id `_user` (the file name of the user's own picture) cannot have a picture |
| `500` | `avatar_storage` | the file could not be written, moved or removed |

The payloads carry the picture: `GET /api/agents`, `GET`/`POST`/`PUT /api/agents/<agent_id>` and the `agents` of `GET /api/me` and `POST /api/login`
have `avatar`, and `GET /api/me` / `POST /api/login` have the user's own `avatar`: `null`, or `{"url", "version", "style"}`, where `style`
is the saved style, `circle` or `free`. The agent's style is its `avatar_style` field ([agent.yaml](agent-yaml.md#avatar_style)); the user's is the
`avatar_style` preference ([preferences](user-yaml.md#preferences-api)). `avatar` is read-only information, not an agent field: a body read with `GET` and sent back with `PUT` is accepted and its `avatar` is ignored (only `avatar_style` is saved).

Files live in `data/<user_id>/.avatars/<agent_id>.png` and `_user.png`, outside every agent's data folder, so the `agent:` source, the
folder tools and the memory tools cannot list or write them.

## Disposable chats

`POST /api/agents/<agent_id>/chats` accepts an optional JSON body `{"disposable": true}` (or
the query `?disposable=true`); without it a normal chat is created. Chat summaries (list,
create, `GET .../chats/<chat_id>`, rename) include `"disposable": bool` and `"expires_at"`:
the ISO time a disposable chat will be deleted (last activity plus the user's
`disposable_retention_days`, default 30), `null` for normal chats. They also carry
`"origin"`: `"web"` (chats created by this API, and chats with no marker) or `"telegram"`
(created by the Telegram bot; stored in a `<chat_id>.origin.json` sidecar). The Telegram bot only
resumes chats whose origin is `telegram`; the API always creates `web` chats.

`POST /api/agents/<agent_id>/chats/<chat_id>/disposable` with `{"disposable": true|false}`
converts the chat in either direction and returns its summary. Becoming disposable drops its
indexed chunks and excludes it from Dream; becoming normal counts it again from the start.
A chat with an active or queued reply returns `409` (`agent_busy`); an unknown chat is `404`
and a non-boolean value `422`. See [disposable chats](../user-guide/memory.md#disposable-chats).

## Following a running reply

Leaving a chat never stops its reply: the run belongs to the service, not to the HTTP request that started it, and only
steering (a new message) or `interrupted` ends it early. Chat summaries (`GET /api/chats`, `GET .../chats`, the `chat` of
`GET .../chats/<chat_id>`) carry `active` (boolean), true while the chat has a reply running or queued.

`GET /api/agents/<agent_id>/chats/<chat_id>/stream` attaches another viewer (another tab, a page load) to the reply that is
running now. It returns the same SSE frames as the `messages` stream (`reasoning`, `text`, `tool_start`, `tool`, `memory`, `approval`,
`approval_resolved`, `task_started`, `notice`, `usage`, `interrupted`, `error`, then `done`, each with the generation `id:`), starting with
the events produced so far (consecutive `reasoning`/`text` chunks arrive merged, and the replay keeps at most the last
5000 events) and continuing live until the reply ends. Viewers do not take events from each other and the stream of the
original `messages` request is unchanged; closing a viewer does not cancel the reply. Codes: `200` SSE; `204` (empty body)
when no reply is running, meaning the saved chat is complete; `404` unknown chat; `401` not logged in. The replay is
dropped when the reply ends, so after `204` read `GET .../chats/<chat_id>`.

### stopping a reply

`POST /api/agents/<agent_id>/chats/<chat_id>/stop` answers `202 {"status": "stopping"}` and cancels the chat's running
reply and any queued ones. The stream of the cancelled reply ends with `interrupted` (the partial answer is kept in the
chat, marked `interrupted`) and `done`. With nothing running it is a no-op that still answers `202`; an unknown chat
is `404`. The web UI's send button becomes Stop while a reply runs and the box is empty (with text it steers).

### Claude plan usage

`GET /api/claude/usage` returns `{"available": true, "windows": [{"key", "percent", "resets_at"}]}` where `key` is
`session` (5 hours), `week`, `week_opus` or `week_sonnet` (only the windows the plan has), `percent` is 0-100 and
`resets_at` an ISO time or `null`. When it cannot be known: `{"available": false, "reason"}` with `no_own_login`,
`no_credentials`, `expired`, `unauthorized` or `unreachable`. Cached one minute per user; the token is never part of the
answer. See [plan usage](../user-guide/claude-subscription.md#plan-usage).

Tool steps are announced live: `tool_start` (`{"id", "name", "arguments"}`) is sent right before a tool runs, and the `tool` event that follows when it finishes (`{"id", "name", "arguments", "result", "error"}`) carries the same `id` (the provider's tool call id, else `call-<n>`). Tools run one after another, so each start is followed by its own final event. A tool that was interrupted never gets a final event: the stream then ends with `interrupted` (or `error`), and clients mark any step still running as stopped. A `tool_start` is part of the replay of `.../stream`, so a viewer that attaches mid-run sees the running steps. Stored `actions` are unchanged (finished tools only, without `id`), and Telegram ignores `tool_start`.

Stored assistant messages (in `GET /api/agents/{agent_id}/chats/{chat_id}`) may include `timeline`: the turn's segments in order, `[{"type": "reasoning", "text": "..."}, {"type": "tool", "index": 0}, {"type": "text", "text": "..."}]`, where `index` points into the message's `actions`. It is absent for turns without thinking or tools and for turns saved before 0.1.2.

```
GET /api/agents/helper/chats/20260105-093000/stream
event: text
data: "Partial answer so far"

event: text
data: " and the rest, live"

event: usage
data: "context: ..."

event: done
data: ""
```

Pending approvals are visible both ways: replayed as `approval` events and listed by `GET .../approvals`.

## Terminal approvals

Agents with the [terminal tool group](../user-guide/terminal.md) pause a reply while an action waits for the user. Two extra
SSE events travel on `POST .../chats/<chat_id>/messages` (same framing as the others: `event`, `data` as JSON, `id` = the
generation id):

```
event: approval
data: {"id": "9f2c1a7be034", "tool": "terminal", "backend": "pc", "command": "rm -rf build", "cwd": "~/workspace", "reason": "recursive delete", "risk": "dangerous", "timeout_seconds": 300, "expires_at": 1767225900.5, "chat_id": "20261104-101500-a1b2c3", "agent_id": "aria", "options": ["once", "session", "agent"]}

event: approval_resolved
data: {"id": "9f2c1a7be034", "decision": "approved", "reason": "approved", "scope": "once"}
```

`backend` is the name of the user's terminal backend the action targets (the `approvals` mode of that backend decided to ask). `risk` is `dangerous` (flagged by the policy) or `manual` (the backend's approvals are `manual`). `tool` is `terminal`,
`sandbox_read`, `sandbox_write`, `sandbox_patch` or `sandbox_search`, or one of the `permissions` or `agents` tools (`agent_set_level`, `agent_set_tool_groups`, `agent_add_rule`, `agent_remove_rule`, `agent_reset_rules`, `agent_create`, `agent_update`, `agent_rename`, `agent_delete`: `backend` and `cwd` are empty, `command` is the multi-line diff, `options` is always `["once"]` and a `session` or `agent` answer is refused); `command` is the command line or a short description of the
file operation. `decision` is `approved` or `denied`; `reason` is `approved`, `denied by user`, `timed out`, `cancelled` or
`service shutdown`. `options` lists the approve scopes the card offers: `once` always, `session` and `agent` when
[the action may be remembered](../user-guide/terminal.md#remembering-an-approval) (a dangerous action only has `once`); `scope` of the
resolved event is what was granted (`once`, `session`, `agent`, `deny` for a refusal). The stream stays open and silent between the two events, and the reply resumes by itself afterwards.

`task_started` (`{"task_id", "goal", "title", "chat_id"}`, `chat_id` of the worker) is sent as soon as a task launched by this reply exists, so a `task_start` call that is still running (`wait=true` blocks until the worker ends) can already show its card with the Open chat button; the same stream also has the final `tool` event with the result.

`GET /api/agents/<agent_id>/chats/<chat_id>/approvals` returns the chat's pending approvals (same objects as the `approval`
event; `[]` when none), for clients that connect after the event was sent.

`POST /api/agents/<agent_id>/chats/<chat_id>/approvals/<approval_id>` with `{"decision": "approve" | "deny", "scope": "once" | "session" | "agent"}`
answers one. `scope` defaults to `once` and is ignored for `deny`:

```json
{"status": "approved", "scope": "session"}
```

(`"denied"` and `"scope": "deny"` for `deny`.) `session` remembers that exact action for this chat; `agent` also saves a narrow
`allow` rule in the agent's `approval_rules` and updates the running agent. A scope the card did not offer in its `options` is a `400`
and the card stays open. When the agent rule cannot be saved (the blanket-allow guardrail, an unwritable file or audit trail) the action
is **approved once**, `scope` is `once` and the response has `error` with the reason; nothing is written. Answering again with the same user is safe and returns the original outcome.

| status | when |
| --- | --- |
| `200` | answered (or already answered by this user) |
| `403` | the approval belongs to another user |
| `404` | unknown, expired, timed-out or cancelled approval, or the chat/agent is not the user's (`code: "unknown_approval"` for the first group) |
| `400` | the `scope` is not one of the card's `options` |
| `422` | `decision` is missing or not exactly `approve` / `deny`, or `scope` is not `once` / `session` / `agent` |

Telegram answers the same approvals with inline buttons (callback data `ap:<approval id>:y|s|a|n`, well under Telegram's 64 bytes).

### Permission levels in the web UI

| call | result |
| --- | --- |
| `GET /api/levels` | read only, any logged-in user. `levels`: the six levels in order, each `{name, number, title, parts, description, tool_groups}` (`parts` are the texts of the services the level adds over the one below, `description` joins them; the texts are English and have Spanish translations in `GET /api/localization`); `groups`: tool group id -> minimum level; `services`: every service key of `SERVICE_MIN_LEVEL` -> level; `backend_rules`: `{local, ssh_root, ssh}` -> level of each backend class; `mcp` and `delegation_targets` (their minimum level); `max_level`: the caller's maximum. Built from `src/config/levels.py`, `ceiling.py` and `levels_help.py`, so it is what is enforced. The agent form computes the level its choices need from it |
| `GET /api/agents/<agent_id>/approval-rules/effective` | owner only (another user's agent is a `404`, an administrator's included). `{layers: [{name: installation\|user\|agent, default, rules: [{index, learned, rule}]}], shorthand: {backend: manual\|smart\|off\|null}, default}`: the three rule layers least to most specific, the legacy per-backend `approvals` shorthand (`null`: the agent file does not set it) and the default that applies when no rule matches |
| `GET /api/admin/users` | administrators only (`max_level` `admin`; `403 admin_required` for anyone else, `401` without a session). Every user: `{id, name, max_level, agent_count, agents: [{id, name, level, declared}], min_max_level, self}`, or `{id, error}` for a `user.yaml` that does not load. `level` is the higher of the declared and the required level; `min_max_level` the lowest maximum that still fits the agents |
| `PUT /api/admin/users/<user_id>/max-level` | body `{"max_level": "<level>"}` (unknown name: `422`); administrators only. Writes the line `max_level` of that user's `user.yaml` (comments and other keys stay) under the user's lock; effective on their next request. Refused: your own user (`400 max_level_self_change`), a maximum below the level of one of the user's agents (`409 max_level_below_agents` with the list; nothing is written), an unknown user (`404`). Returns the user's row as in the list. A logger line records who changed what |

This is the only route that sets a `max_level`; every other body that carries one is a `422`.

### Approval rules in the agent body

`POST`/`PUT /api/agents` accept the agent layer `approval_rules` (`{default, rules}`, fields in [terminal](../user-guide/terminal.md#approval-rules)).
The agent form edits its hand-written rules through `PUT`: a body without `approval_rules` keeps the stored list (learned rules included),
`{"rules": []}` clears it, and a list replaces it (the form sends its rules followed by the learned ones). An invalid rule is a `422`
naming the field, the blanket-allow guardrail a `400` with `approval_rule_not_allowed`; nothing is written in either case.

### Learned approval rules

The rules in the agent file's `approval_rules` (the ones "Always for this agent" appends, and hand-written ones), for the logged-in
user's own agents only (another user's agent is a `404`):

| call | result |
| --- | --- |
| `GET /api/agents/<agent_id>/approval-rules` | `[{"index": 0, "learned": true, "rule": {...}}]`; `learned` is true for rules written from a card |
| `DELETE /api/agents/<agent_id>/approval-rules/<index>` | removes the rule at that position; returns the list that remains; `404` for a position that does not exist |
| `DELETE /api/agents/<agent_id>/approval-rules` or `POST .../approval-rules/reset` | removes every rule of the agent's list (its `default` stays); returns `[]` |

Each removal writes an `approval_rule` line to the agent's [audit file](../user-guide/terminal.md#audit). The agent form never overwrites
these rules: a `PUT /api/agents/<agent_id>` without `approval_rules` keeps the stored ones (`{"rules": []}` clears them). A running reply keeps the rules it
started with; the next reply uses the file.

## Subagent tasks

Tasks launched by the `delegate` tools (see [delegating tasks](../user-guide/delegation.md)). All routes need the session and only ever see the session user's tasks; another user's task id answers `404` like an unknown one.

| route | what it does |
| --- | --- |
| `GET /api/tasks?agent=<agent_id>&chat=<chat_id>&status=<running\|finished>&limit=<n>` | the user's tasks, oldest first; with `agent` and `chat`, only those launched from that parent chat; `status` keeps the running or the finished ones, `limit` (0-500) the newest N. Without both `agent` and `chat` (the task panel's cross-chat list) each task's `parent` also has `agent_name` and `title` (the parent chat's title; empty for an unattended task without a parent chat). Tasks also carry `unattended`, `trigger` and `created_at`, and `activity` (what a running task's worker is doing now, `{kind: "reasoning" \| "text" \| "tool", tool, detail}` with `detail` the first line of the tool's main argument, at most 80 characters; `null` when not running in this process or nothing happened yet). Each task: `id`, `title`, `goal` (first 300 characters), `target` (`{kind: "ephemeral" \| "agent", agent_id}`), `status` (`running`, `done`, `failed`, `cancelled`, `timed_out`), `reason`, `started_at`, `ended_at`, `elapsed_s`, `usage` (`{in, out}`), `parent` (`{agent_id, chat_id}`), `child` (`{agent_id, chat_id}` of the hidden worker chat), `result_preview` (300 characters), `result_truncated`, `error`. With both filters the response also has `active` (the parent chat has a reply running or queued) and `pending_wake` (a finished task's result is still on its way to the parent): the web UI polls every ~3 s while a task runs or `pending_wake` is true, and attaches to the wake reply through `GET .../stream` when `active` turns true. |
| `GET /api/tasks/<task_id>` | the same record with the full `goal` and the capped `result` (50,000 characters) |
| `POST /api/tasks/<task_id>/steer` | body `{"text": "..."}`: the user's correction goes into the running task, like the `task_steer` tool (the worker's reply is interrupted and kept, the correction appended). Returns `{id, status}` |
| `POST /api/tasks/<task_id>/stop` | stops a running task (its partial result is kept) and returns `{id, status}`; on a task that already ended it returns its status unchanged |
| `GET /api/tasks/<task_id>/stream` | read-only live view of a running task's worker reply: the same SSE frames as `GET .../chats/<chat_id>/stream` (replay of what was produced so far, then live `reasoning`, `tool_start`, `tool`, `text`), ended by `done` when the reply ends. Nothing running: only `done`. A task executed by the other process (Telegram or web): one `unavailable` frame, then `done` (no cross-process streaming; the UI falls back to polling the saved chat). Another user's task: 404 |
| `GET /api/tasks/<task_id>/chat` | the hidden worker chat, read-only, in the shape of `GET .../chats/<chat_id>` plus `task: {id, agent_id, chat_id, internal: true}`. There is no write route: the worker chat never appears in the chat lists |

Errors: `401` no session, `404` unknown task (`code: "unknown_task"`) or a task of another user, `400` empty or too long steer text, `409` steer or stop on a task that does not run in this process (already ended, or owned by another process). A wake turn in `GET .../chats/<chat_id>` has `role: "user"`, `source: "task"` and `task_ids`; the UI shows it as a "Task result" note, not as a message of the user.

## Editing a message

`GET /api/agents/<agent_id>/chats/<chat_id>` returns each message with an `index`: its
position among the stored turns of the chat file (tool turns count, so indexes can skip).

`POST /api/agents/<agent_id>/chats/<chat_id>/messages` accepts an optional
`"edit_index": <int>` next to `content` and `attachments`. The index must belong to a
stored `user` turn. The service cancels and discards the chat's active and queued replies,
removes that turn and everything after it (kept in `<chat_id>.removed.json`), drops the
cached agent context and answers `content` as a normal message, streaming the usual SSE
events. When `attachments` is empty the original turn's attachments are kept. An index that
is out of range or not a user turn returns `400` with `code: "invalid_edit_target"`; a
negative or non-integer value is a `422`; an unknown chat is `404`; a cancelled reply that
does not stop within 30 s gives `409` (`agent_busy`). Long-term memory is not rewound.
