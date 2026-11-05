# HTTP API and error codes

The web UI is a thin client of these routes (served by `src/web/app.py`). Everything except login, `GET /api/version` and `GET /api/localization`
needs the signed session cookie and is scoped to the session user. Application errors are `{"detail": "<localized message>"}`
plus an optional `"code"` (see [error codes](#error-codes)).

## route index

| area | routes |
| --- | --- |
| session | `POST /api/login`, `POST /api/logout`, `GET /api/me` (the profile and agents, plus `terminal_backends`: `[{"name", "type", "description"}]` sorted by name, the user's terminal backends for the agent form; never hosts, users or key paths), `GET /api/localization?language=en\|es`, `GET /api/version` (public: `{"version": "0.1.0", "build": null}`; `version` comes from `pyproject.toml`, `build` is the first 7 characters of `RECALL_BUILD` or `null`) |
| preferences | `GET`/`PATCH /api/preferences` ([details](user-yaml.md#preferences-api), including the Telegram orchestrator's `default_agent_id`, `orchestrator` / `core_agent_id` and the read-only `orchestrator_default`; the response also carries a read-only `limits` object `{soul_chars, memory_chars, user_chars}` from the `memory_limits` block of settings.yaml and a read-only `defaults` object with the effective defaults and number bounds), `GET /api/speech-to-text/models` |
| models | `GET /api/providers/status`, `GET /api/models/catalog`, `GET /api/models`, `GET`/`POST /api/models/favorites`, `DELETE /api/models/favorites/<model_id>` ([models and providers](../user-guide/models-and-providers.md)) |
| agents | `GET`/`POST /api/agents`, `GET`/`PUT`/`DELETE /api/agents/<agent_id>`, `GET /api/tool-groups`, `GET /api/agents/<agent_id>/index`, `POST /api/agents/<agent_id>/index/reindex` (table below) |
| MCP | `GET /api/mcp`, `GET /api/mcp/<server_id>/tools`, `POST /api/mcp/<server_id>/connect`, `DELETE /api/mcp/<server_id>/connection` ([MCP servers](../user-guide/mcp.md)) |
| chats | `GET /api/chats`, `GET`/`POST /api/agents/<agent_id>/chats`, `POST .../chats/<chat_id>/disposable`, `GET`/`DELETE .../chats/<chat_id>`, `GET .../chats/<chat_id>/models`, `PUT .../model`, `PUT .../reasoning-effort`, `PUT .../title`, `POST .../chats/<chat_id>/messages` (SSE, see [architecture](../architecture/overview.md#web-steering)), `GET .../chats/<chat_id>/stream` ([following a running reply](#following-a-running-reply)), `GET .../chats/<chat_id>/approvals`, `POST .../chats/<chat_id>/approvals/<approval_id>` ([terminal approvals](#terminal-approvals)) |
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

New codes are set with `UserMessageError(..., code="...")` or a class-level `code`.

## agent routes

| method | route | result |
| --- | --- | --- |
| `GET` | `/api/tool-groups` | shared tool-group catalog available to the user's agents |
| `GET` | `/api/mcp` | shared MCP server catalog with the user's connection status |
| `GET` | `/api/mcp/<server_id>/tools` | tools of a server the user has connected (`409` when not connected) |
| `POST` | `/api/mcp/<server_id>/connect` | starts the server's sign-in for the user; returns the status and sign-in URL |
| `DELETE` | `/api/mcp/<server_id>/connection` | stops the user's server and deletes their stored credentials |
| `GET` | `/api/agents` | full configuration of each agent the user owns, including `chat_config.tool_groups` |
| `GET` | `/api/agents/defaults` | defaults and bounds of the agent form fields, generated from the configuration models: `{chat_config, dream, terminal}`, each field `{default, min, max}` (`null` where the model has no default or bound), e.g. `dream.max_steps` `{default: 60, min: 1, max: 500}`, `chat_config.conversation_messages_retained` `{default: 30, min: 1, max: null}` |
| `POST` | `/api/agents` | creates `agents/<id>.yaml` and appends the id to `agents` in `user.yaml` (`201`) |
| `GET` | `/api/agents/<agent_id>` | one agent's configuration |
| `PUT` | `/api/agents/<agent_id>` | replaces the agent; the body `id` may differ, which renames it |
| `DELETE` | `/api/agents/<agent_id>` | removes the agent from `user.yaml` and deletes its file (`204`) |
| `GET` | `/api/agents/<agent_id>/index` | memory index status: `sources`, `files`, `chats`, `chunks`, `last_reconcile`, `folders_reconciled_at`, `rebuilding`, `busy`, `model`, `error` |
| `POST` | `/api/agents/<agent_id>/index/reindex` | queues a reconcile of every source, folders included (`202`); body `{"full": true}` drops the index and rebuilds it |

The rules these routes follow (ids, renames, deletion, locks, `409` while replies run) are in
[managing agents](../user-guide/agents.md#managing-agents).

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
running now. It returns the same SSE frames as the `messages` stream (`reasoning`, `text`, `tool`, `memory`, `approval`,
`approval_resolved`, `notice`, `usage`, `interrupted`, `error`, then `done`, each with the generation `id:`), starting with
the events produced so far (consecutive `reasoning`/`text` chunks arrive merged, and the replay keeps at most the last
5000 events) and continuing live until the reply ends. Viewers do not take events from each other and the stream of the
original `messages` request is unchanged; closing a viewer does not cancel the reply. Codes: `200` SSE; `204` (empty body)
when no reply is running, meaning the saved chat is complete; `404` unknown chat; `401` not logged in. The replay is
dropped when the reply ends, so after `204` read `GET .../chats/<chat_id>`.

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
data: {"id": "9f2c1a7be034", "tool": "terminal", "backend": "pc", "command": "rm -rf build", "cwd": "~/workspace", "reason": "recursive delete", "risk": "dangerous", "timeout_seconds": 300, "expires_at": 1767225900.5, "chat_id": "20261104-101500-a1b2c3", "agent_id": "aria"}

event: approval_resolved
data: {"id": "9f2c1a7be034", "decision": "approved", "reason": "approved"}
```

`backend` is the name of the user's terminal backend the action targets (the `approvals` mode of that backend decided to ask). `risk` is `dangerous` (flagged by the policy) or `manual` (the backend's approvals are `manual`). `tool` is `terminal`,
`sandbox_read`, `sandbox_write`, `sandbox_patch` or `sandbox_search`; `command` is the command line or a short description of the
file operation. `decision` is `approved` or `denied`; `reason` is `approved`, `denied by user`, `timed out`, `cancelled` or
`service shutdown`. The stream stays open and silent between the two events, and the reply resumes by itself afterwards.

`GET /api/agents/<agent_id>/chats/<chat_id>/approvals` returns the chat's pending approvals (same objects as the `approval`
event; `[]` when none), for clients that connect after the event was sent.

`POST /api/agents/<agent_id>/chats/<chat_id>/approvals/<approval_id>` with `{"decision": "approve" | "deny"}` answers one:

```json
{"status": "approved"}
```

(`"denied"` for `deny`). Answering again with the same user is safe and returns the original outcome.

| status | when |
| --- | --- |
| `200` | answered (or already answered by this user) |
| `403` | the approval belongs to another user |
| `404` | unknown, expired, timed-out or cancelled approval, or the chat/agent is not the user's (`code: "unknown_approval"` for the first group) |
| `422` | `decision` is missing or not exactly `approve` / `deny` |

Telegram answers the same approvals with inline buttons.

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
