# Code map

Paths are relative to the repository root. In Docker the same tree sits under `/app`. Start from the entry points, then follow the imports.

## Entry points

| File | What it starts |
| --- | --- |
| `web.py` | web UI and HTTP API (uvicorn, `src/web/app.py`) |
| `run_telegram.py` | Telegram bot (polling) |
| `cli.py` | terminal chat for `USER_ID`/`AGENT_ID` in `.env` |
| `python -m src.admin` | `version`, `create-user`, `set-password`, `route-test`, `terminal-test` (`src/admin/users.py`); `agent ...`, `user show/set` (`src/admin/agents.py`); `set-secret`, `secret list` (`src/admin/credentials.py`); usage in the skill page admin-cli |
| `main.py` | older LangGraph prototype (`src/agent/graph.py`, `src/embeddings/data_source.py`); not the live path |

## Agent runtime (`src/agent/`)

| Path | Role |
| --- | --- |
| `src/agent/agent.py` | `Agent`: context, system prompt, streaming reply loop, tool execution, memory events |
| `src/agent/profile.py` | reads SOUL, memories, skills, for_user.md; builds the system-prompt blocks |
| `src/agent/default_soul.md` | template SOUL.md given to new agents |
| `src/agent/default_soul.py` | loads that template |
| `src/agent/templates.py` | agent templates for `agent create --from-template` and `agent_create` (`default`, `guide`, `permission-manager`; SOULs in `src/agent/*_soul.md`) |
| `src/agent/tools.py` | `build_tools`: assembles tools from groups, `allow`/`deny`, disposable rules |
| `src/agent/tool_groups.py` | tool-group catalog and which tools each group holds |
| `src/agent/tooling.py` | the `@tool` decorator and schema generation |
| `src/agent/toolsets/memory.py` | `query_memory`, `search`, `list_memories`, `read` |
| `src/agent/toolsets/tags.py` | `list_tags`, `find_notes_by_tag` |
| `src/agent/toolsets/sessions.py` | `session_search` |
| `src/agent/toolsets/editing.py` | `write`, `append`, `edit`, `move`, `delete` |
| `src/agent/toolsets/self_files.py` | `memory`, `skill_view`, `skill_manage`, `soul_edit` |
| `src/agent/toolsets/folders.py` | the `folder_*` tools |
| `src/agent/toolsets/web.py` | `web_search`, `web_retrieve` |
| `src/agent/toolsets/terminal.py` | terminal tools and the prompt notice for them |
| `src/agent/toolsets/delegate.py` | the seven `task_*` tools, the delegation limits and the prompt notice for them |
| `src/agent/toolsets/agents.py` | the `agents` tools (built like the `permissions` ones) |
| `src/service/agent_admin.py` | the checks, plans and audit behind the `agents` tools |
| `src/agent/toolsets/mcp.py` | MCP tools |
| `src/agent/toolsets/dream.py` | dream review-queue tools |
| `src/agent/associative.py` | associative-memory query and fragment formatting |
| `src/agent/dream.py` | `DreamRunner`: one dream session and its toolset |
| `src/agent/usage.py` | token/context accounting for the usage report |

## Service and channels

| Path | Role |
| --- | --- |
| `src/service/chat.py` | `ChatService`: agents, chats, send queue, preferences, models, dream commands |
| `src/service/dream.py` | `DreamManager`: schedule, lock, sessions, command parsing |
| `src/service/providers.py` | which providers are available and why not |
| `src/service/tasks.py` | `TaskStore` (`data/<user_id>/.tasks/`) and `TaskManager`: subagent tasks, limits, watchdog, commands inbox, wake claim and poller across processes |
| `src/web/app.py` | FastAPI routes (agents, chats, SSE messages, dream, soul, index) |
| `src/web/auth.py` | password hashing and verification |
| `src/web/static/app.js` | browser UI logic |
| `src/web/static/index.html` | browser UI markup |
| `src/web/static/style.css` | browser UI styles |
| `src/telegram/bot.py` | Telegram handlers, commands, settings panel, approvals buttons |

## Configuration (`src/config/`)

| Path | Role |
| --- | --- |
| `src/config/models.py` | pydantic models of every yaml: `AgentConfig`, `ChatConfig`, `DreamConfig`, `UserConfig`, `ResolvedAgent`... |
| `src/config/loader.py` | `load_user`, `load_agent`: yaml plus environment into a `ResolvedAgent` |
| `src/config/agents.py` | `AgentStore`: atomic create/update/delete of agent yaml |
| `src/config/settings.py` | reads the root `settings.yaml` into module constants |
| `src/config/preferences.py` | per-user `preferences.json` store |
| `src/config/favorites.py` | favorite models store |
| `src/config/model_refs.py` | `provider:model` reference parsing |
| `src/config/zdr.py` | zero-data-retention decision |
| `settings.yaml` | framework defaults |

## Data plane (`src/core/`)

| Path | Role |
| --- | --- |
| `src/core/sources/registry.py` | `SourceRegistry`: the per-agent access point |
| `src/core/sources/base.py` | `Source` base: path guard, read/write/list/walk |
| `src/core/sources/agent_files.py` | SOUL, history, memories, skills (and the built-in skill hook) |
| `src/core/sources/mind_palace.py` | the notes source |
| `src/core/sources/conversations.py` | chats as JSONL plus sidecars (title, model, disposable and hidden flags) |
| `src/core/sources/dream.py` | dream state, traces, lock |
| `src/core/sources/folders.py` | user folders |
| `src/core/index.py` | vector index (Chroma), reconcile and reindex |
| `src/core/chunking.py` | markdown and conversation chunking |
| `src/core/review.py` | dream review queue |

## Providers (`src/providers/`)

| Path | Role |
| --- | --- |
| `src/providers/provider.py` | `LLMProvider` interface, `get_provider` |
| `src/providers/routing.py` | which provider serves a model reference |
| `src/providers/openrouter.py` | OpenRouter chat, embeddings, speech-to-text |
| `src/providers/ollama.py` | Ollama chat and embeddings |
| `src/providers/claude_subscription/provider.py` | Claude Code subscription chat |
| `src/providers/claude_subscription/usage.py` | plan usage (session/week %) from the login's OAuth token; `ChatService.claude_usage`, `GET /api/claude/usage` |
| `src/providers/catalog.py` | OpenRouter model catalog |
| `src/providers/tavily.py` | web search and extraction client |
| `src/providers/mcp.py` | MCP manager and connections |
| `src/providers/telegram_notify.py` | Telegram notifier (used for dream notifications) |
| `src/providers/cancellation.py` | cancellation token for replies |
| `src/providers/transport.py` | shared HTTP transport |

## Terminal (`src/terminal/`)

| Path | Role |
| --- | --- |
| `src/terminal/policy.py` | hardline and dangerous command rules |
| `src/terminal/session.py` | `TerminalSession`: policy, approval, execution, audit |
| `src/terminal/approvals.py` | approval broker (web and Telegram answers) |
| `src/terminal/backends.py` | ssh and local execution |
| `src/terminal/resolve.py` | agent backends resolved against the user's `terminal_backends` |
| `src/terminal/files.py` | `sandbox_*` file operations |
| `src/terminal/output.py` | ANSI stripping, redaction, truncation |
| `src/terminal/audit.py` | `terminal_audit.jsonl` writer |

## Utilities and this knowledge

| Path | Role |
| --- | --- |
| `src/utils/logger.py` | per-run log folders |
| `src/utils/i18n.py` | English/Spanish message catalog and `InputError` |
| `src/utils/files.py` | atomic writes and file locks |
| `src/utils/pdf.py` | PDF text and OCR |
| `src/utils/media.py` | video frame and audio extraction |
| `src/knowledge/__init__.py` | loader of this built-in skill |
| `src/knowledge/recall/SKILL.md` | this skill's hub |

## Where to look for...

- why a prompt looks the way it does: `src/agent/profile.py`, then `src/agent/agent.py`
- why a tool is missing: `src/agent/tools.py`, `src/agent/tool_groups.py`, `src/config/loader.py`
- why a config is rejected: `src/config/models.py`
- why a write is refused: `src/agent/toolsets/common.py` and the `Source` flags in `src/core/sources/registry.py`
- why a command was denied: `src/terminal/policy.py`, `src/terminal/session.py`
- tests: `tests/` (standard `unittest`, one file per area)
