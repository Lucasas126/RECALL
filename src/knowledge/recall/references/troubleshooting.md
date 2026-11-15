# Troubleshooting

Only the user (or administrator) can fix configuration, keys and services. Your job: find the evidence, explain it, say exactly what to change.

## Logs

Each process run writes a new folder `logs/<YYYYmmdd_HHMMSS+zzzz>_<run>/` where `<run>` is `web`, `telegram` or `cli` (Docker: `/app/logs`, mounted from `RECALL_LOGS_DIR`). Inside: `recall.log` with everything, plus one file per component (for example `sources.log`). Level is DEBUG; files roll at UTC midnight. Look in the newest folder of the process involved. Lines look like `time LEVEL recall.<component>: message`. Terminal actions are in `data/<user_id>/<agent_id>/terminal_audit.jsonl`; dream traces in `data/<user_id>/<agent_id>/dream/sessions/`.

## Error cards in the web chat

| Code | Meaning | The user should |
| --- | --- | --- |
| `no_agents` | the user has no agents | create an agent in the Agents tab |
| `unknown_agent`, `unknown_chat` | not found or not the user's | start a new chat or pick another agent |
| `missing_api_key` | the provider key variable is not set on the server | set the variable named in `provider.api_key` (user yaml): either `python -m src.admin set-secret NAME` in his own terminal (no restart) or the server environment/`.env` (restart) |
| `provider_unreachable` | the Ollama/provider server did not answer | check the server is running and `ollama.host` / `OLLAMA_HOST` |
| `claude_not_installed` | Claude Code is missing on the RECALL host | install it, or choose another model |
| `claude_no_own_login` | no `claude_subscription.claude_config_dir` inside the user's own config folder, and not in `claude_subscription.default_login_users` (`settings.yaml`) | ask the administrator for a login folder |
| `claude_not_logged_in` | Claude Code is logged out | run `claude auth login` on the RECALL host |
| `provider_unavailable` | Claude Code did not report its login state | run `claude auth status` on the host |
| `model_unavailable` | the model is not in the provider's catalog | choose another model |
| `rate_limited` | the upstream provider answered 429 | wait, or choose another model |
| `agent_busy` | active or queued replies exist (also blocks editing or deleting the agent) | wait, then retry |
| `mcp_not_connected` | an MCP tool needs an account not connected | connect it in Settings |

## Checking a provider

`GET /api/providers/status` returns each provider with `available` and, when it is not, a `reason` (`no_api_key`, `unreachable`, `cli_missing`, `logged_out`, `cli_unknown`); the model picker refuses an unavailable provider with the same message. A key variable that is unset on the server is the usual cause: the YAML holds only the variable's NAME. Never ask the user to paste a key into a chat.

## `python -m src.admin` fails with a missing module

`ModuleNotFoundError: No module named 'yaml'` (or another dependency) means the shell's `python` is not the app's. Use the project's virtualenv interpreter, relative to the repo root: `.venv/bin/python -m src.admin ...` on Linux, `.venv/Scripts/python -m src.admin ...` on Windows. In the container `python` is the app's.

## `--set` says "Input should be a valid string"

Values are parsed as YAML: wrap text containing `": "` in quotes. String fields such as `description` take the value raw in current versions.

## Agent will not load or save

- Unknown keys are rejected in the agent yaml. A leftover `vault` key must become `folders: [{name: vault, path: ...}]`.
- `dream.tools: [terminal]` and `terminal.allow_in_dream` are rejected: dreams have no terminal.
- `terminal.default_backend` is required with several backends and must be one of them; every backend key must exist in the user's `terminal_backends`.
- Enabled MCP servers must exist in the `mcp.servers` catalog of `settings.yaml`.
- A folder path must exist when saving; if it disappears later (unmounted drive), the agent loads without it and the log says `folder ... not found ... skipped`.

## Missing tools

- No `web_search`/`web_retrieve`: the Tavily key (`tavily.api_key` variable) is not set, or `web` is not in `tool_groups`.
- No folder tools: the folder groups need at least one folder in `folders`.
- No terminal tools: `terminal` group not selected, no `terminal` block, or no backend resolved (the log says `terminal tools disabled`); `local` backends need `terminal.allow_local`. They are also absent in disposable chats.
- No tools at all: `chat_config.tools` is false.

## Telegram

- Only one process can poll a bot token: do not run a local `run_telegram.py` beside a server one with the same `TELEGRAM_BOT_TOKEN`.
- The bot refuses to start without `TELEGRAM_BOT_TOKEN`. Unknown Telegram ids are rejected: `TELEGRAM_ALLOWED_USERS` must hold `<telegram_id>=<user_id>`.
- After renaming or deleting an agent, an open Telegram session keeps the old id: use `/selectagent` or restart the bot.

## Memory index

Search results look stale or empty: in the agent's Folders tab check the index status and press Reindex (the API is `POST /api/agents/<agent_id>/index/reindex`, with `full` to drop and rebuild). The index also rebuilds by itself after a change of embeddings model or chunk sizes; until it finishes, searches return only what is already indexed. If associative memory shows "skipped", the query model's key variable is unset or its request failed (see the log).

## Dream not running

- `dream.enabled` is false: only manual starts work. A schedule needs `every_hours` or `daily_at` (not both).
- Scheduled runs are skipped while no chat is pending review, and while another session holds `dream/.lock` ("a dream is already running").
- The schedule is checked every minute by whichever process (web or Telegram) is running; with both stopped nothing runs.
- A session ends as `budget_exhausted` or `timed_out` when it hits `dream.max_tokens` or `dream.max_minutes`; the summary in Dream sessions says which.
- Disposable chats are never reviewed.

## Memory full

The `memory` tool refuses a write that would exceed the limit and lists the current entries; send one batch of `operations` that removes or shortens stale entries and adds the new one.

## The answer after a tool call is not streamed

With Claude Sonnet 5.5 (and often Opus), the text written right after a tool result arrives in one block when it is finished: the provider sends it that way (a known upstream behaviour, not RECALL). Thinking and the first answer of a turn still stream; Haiku streams after tools too. Nothing to fix on our side.

## The turn was cut after many tool calls

A turn may do at most `agent.max_tool_hops` tool round-trips (`settings.yaml`, 50). Two rounds before the cut the model gets a system note with how many are left; after the last one it gets "Stop calling tools. Answer the user now using what you already have." and must answer without tools. An agent can override the limit with `chat_config.max_tool_hops` (1-100, empty = the application value; Agents tab, "Tool rounds per turn"). Raise it for long coding or research agents; do not raise it to hide a loop.
