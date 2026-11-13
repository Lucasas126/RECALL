# Architecture

## Layers

```
channels:  web (src/web)  telegram (src/telegram)  cli (cli.py)
              |                |                       |
           ChatService (src/service/chat.py)  <- cli talks to Agent directly
              |
           Agent (src/agent/agent.py)  ->  SourceRegistry (src/core/sources/registry.py)
```

- Channels are thin adapters: they map an identity to a `user_id` (web: Argon2 password + signed session cookie; Telegram: `TELEGRAM_ALLOWED_USERS` entries `<telegram_id>=<user_id>`; CLI: `USER_ID`/`AGENT_ID` from `.env`) and render events. They hold no backend logic.
- `ChatService` validates the user, agent ownership and chat ids, keeps one `Agent` runtime per (user, agent, chat), and queues replies.
- Data access goes only through `SourceRegistry`; capabilities receive their sources by injection, not through globals.

## Processes

| Process | Entry point | Log run name |
| --- | --- | --- |
| Web UI + HTTP API | `web.py` (uvicorn, host/port from `web` in `settings.yaml`) | `web` |
| Telegram bot (polling) | `run_telegram.py` | `telegram` |
| Terminal chat | `cli.py` | `cli` |

Web and Telegram may run side by side on the same folders; file locks coordinate atomic writes (agent yaml, preferences, memory files, index). Only one process can poll a Telegram bot token. A dream schedule runs in whichever process is up.

## Folders

Paths come from `paths` in `settings.yaml` (defaults `.config`, `data`, `logs`); in Docker they are `/app/.config`, `/app/data`, `/app/logs`, mounted from the host.

```
.config/<user_id>/user.yaml                    user, provider, models, terminal_backends
.config/<user_id>/agents/<agent_id>.yaml       one file per agent
.config/<user_id>/preferences.json             interface preferences shared by web and Telegram
data/<user_id>/.tasks/                         subagent task records (one JSON per task; see delegation.md)
data/<user_id>/<agent_id>/
  SOUL.md  soul_history/  memories/  skills/   your own files (source `agent`)
  mind_palace/  conversations/                 your notes and chats
  dream/                                       dream state; tools cannot read it
  .index/                                      vector index (Chroma)
  terminal_audit.jsonl                         one JSON line per terminal action
logs/<timestamp>_web|_telegram|_cli/recall.log
```

Everything is scoped per user and per agent. Users never see each other's agents or data; agents do not share anything implicitly.

## Sources

Addresses are `source:path`. Sources: `mind_palace` (your notes, writable), `conversations` (chats), `agent` (SOUL, memories, skills; edited with the self tools), `dream` (state), plus one source per user folder named after the folder. Tools only reach sources the agent's tool groups allow; `SourceRegistry` rejects paths that escape a source.

## Request flow of a chat message

1. The channel authenticates the user and calls `ChatService.send` (web: SSE stream; Telegram: updates).
2. The service validates input, converts attachments (audio is transcribed, video split into frames and audio, PDFs replaced by extracted text) and enqueues the message on the worker of that (user, agent). A new message to a chat that is still replying cancels the unfinished reply and keeps the partial text.
3. `Agent.from_config` loads the resolved agent (`src/config/loader.py`), builds the sources, and `load_profile` reads SOUL, memories, skills index and open `for_user.md` items into the system prompt.
4. Per turn, associative memory (if enabled) searches the vector index for fragments; with `parallel` timing it runs beside the model call.
5. `Agent.invoke` calls the provider with the context and tools; tool calls run (at most `agent.max_tool_hops` round-trips from `settings.yaml`), results return to the model, until a final answer.
6. The exchange is appended to `conversations/`; the index is updated in the background after the reply; the service emits events (`reasoning`, `text`, `tool`, `usage`, `interrupted`, `notice`, `memory`).

## Config loading

`load_agent` reads `user.yaml` and the agent yaml, resolves the provider key from the environment and returns a `ResolvedAgent`. Saving an agent through the UI/API is refused while its replies are active or queued; the cached runtime is rebuilt on the next message. `settings.yaml` is read at start-up: restart both processes after editing it.
