# Tests layout

Tests are grouped by domain. A file goes in the folder of the code it exercises; when a test
crosses domains (for example a web API test for approvals), the domain folder wins over `web/`.

| Folder | What it covers |
|---|---|
| `agent/` | agent loop, associative memory, compaction, prompt cache, tool hops, memory tools, timeline |
| `agent/dream/` | dream sessions: scheduler, limits, workers, compaction, API |
| `approvals/` | approval rules, approval memory, explanations |
| `permissions/` | levels, ceiling, permission UI and CLI, permission tool escalation, secrets |
| `terminal/` | terminal tools, backends, processes, protected paths, hardening, file sending to backends |
| `tasks/` | subagents, async tasks, steering, task panel and Telegram cards |
| `providers/` | provider routing, catalog, MCP, cancellation, Telegram transport |
| `providers/claude/` | Claude subscription provider |
| `chat/` | chat service: titles, windows, model picker, edit, summaries, disposable chats |
| `config/` | agent config, settings, preferences, defaults, user models, localization |
| `admin/` | admin CLI: users and agents |
| `avatars/` | avatar images, paths and API |
| `automations/` | automation tools, service and web |
| `telegram_bot/` | Telegram bot behavior (not `telegram/`: that name would shadow the library) |
| `web/` | web app API and static assets (theme, icons, media) |
| `web/js/` | all Node tests (`*.cjs`) plus `test_node_suite.py`, which runs them in the Python suite |
| `knowledge/` | recall docs, index, chunking, search paging, session search |
| `tools/` | tool implementations: pdf, view_image, identity, agents tool, notify, web search filters |
| `meta/` | cross-cutting: version, entrypoint environment |
| `support/` | shared helpers and fakes (`auth_helpers`, `admin_helpers`, `terminal_helpers`...) |

Run everything: `RECALL_SESSION_SECRET=test python -m unittest discover -s tests`
(use `-t .` too if a module cannot be imported). One area: `discover -s tests/terminal -t .`.
Node tests alone: `node --test tests/web/js/*.cjs`.

Every folder is a package (`__init__.py`). Import helpers and other test modules by package path,
for example `from tests.support.auth_helpers import login`.
