# HTTP API and error codes

The web UI is a thin client of these routes (served by `src/web/app.py`). Everything except login and `GET /api/localization`
needs the signed session cookie and is scoped to the session user. Application errors are `{"detail": "<localized message>"}`
plus an optional `"code"` (see [error codes](#error-codes)).

## route index

| area | routes |
| --- | --- |
| session | `POST /api/login`, `POST /api/logout`, `GET /api/me`, `GET /api/localization?language=en\|es` |
| preferences | `GET`/`PATCH /api/preferences` ([details](user-yaml.md#preferences-api)), `GET /api/speech-to-text/models` |
| models | `GET /api/providers/status`, `GET /api/models/catalog`, `GET /api/models`, `GET`/`POST /api/models/favorites`, `DELETE /api/models/favorites/<model_id>` ([models and providers](../user-guide/models-and-providers.md)) |
| agents | `GET`/`POST /api/agents`, `GET`/`PUT`/`DELETE /api/agents/<agent_id>`, `GET /api/tool-groups`, `GET /api/agents/<agent_id>/index`, `POST /api/agents/<agent_id>/index/reindex` (table below) |
| MCP | `GET /api/mcp`, `GET /api/mcp/<server_id>/tools`, `POST /api/mcp/<server_id>/connect`, `DELETE /api/mcp/<server_id>/connection` ([MCP servers](../user-guide/mcp.md)) |
| chats | `GET /api/chats`, `GET`/`POST /api/agents/<agent_id>/chats`, `GET`/`DELETE .../chats/<chat_id>`, `GET .../chats/<chat_id>/models`, `PUT .../model`, `PUT .../reasoning-effort`, `PUT .../title`, `POST .../chats/<chat_id>/messages` (SSE, see [architecture](../architecture/overview.md#web-steering)) |
| dream and soul | `.../dream/settings\|status\|start\|cancel\|sessions`, `.../soul`, `.../soul/history`, `.../memories/<target>` ([dream mode](../user-guide/dream.md#web-api)) |

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
| `agent_busy` | the agent or chat still has active/queued replies | none (wait) |
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
| `POST` | `/api/agents` | creates `agents/<id>.yaml` and appends the id to `agents` in `user.yaml` (`201`) |
| `GET` | `/api/agents/<agent_id>` | one agent's configuration |
| `PUT` | `/api/agents/<agent_id>` | replaces the agent; the body `id` may differ, which renames it |
| `DELETE` | `/api/agents/<agent_id>` | removes the agent from `user.yaml` and deletes its file (`204`) |
| `GET` | `/api/agents/<agent_id>/index` | memory index status: `sources`, `files`, `chats`, `chunks`, `last_reconcile`, `folders_reconciled_at`, `rebuilding`, `busy`, `model`, `error` |
| `POST` | `/api/agents/<agent_id>/index/reindex` | queues a reconcile of every source, folders included (`202`); body `{"full": true}` drops the index and rebuilds it |

The rules these routes follow (ids, renames, deletion, locks, `409` while replies run) are in
[managing agents](../user-guide/agents.md#managing-agents).
