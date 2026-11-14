# backlog

One list of ideas and planned work, with what is already done kept separate. Implemented behaviour is documented in the
[user guide](../README.md); nothing under **planned** exists in the code yet (checked against the code when this page was written).

## planned

### automation and behavior

- **Automations** are being built in steps: the service (triggers `at`, `every`, `cron`, `process_end`; guards; catch-up) is done, see [automations](../user-guide/automations.md); the agent tools (group `automations`, level `assistant`) and the web tab (a second tab of the Tasks dialog) are done too. Left for later: sending a result to a chat. Calendar and conversation events are out of scope.
- **Reminders and proactive messages**: a one-off message the agent sends while it works already exists (`notify_telegram`, see [done](#done)); what is planned is the agent *scheduling* a message ("remind me on Friday...") sent later through Telegram and shown in the web, reusing the dream scheduler. The agent schedules a message ("remind me on Friday...") sent later through Telegram and shown in the web, reusing the dream scheduler. First step towards automations.
- **Automations**: rules and actions based on conversation or calendar events.
- **Recall mode**: the agent enters its mind palace with a context trimmed to the last messages, searches and reads what it needs, and returns only the relevant information, dropping the retrieval process from its context. Complements [dream mode](../user-guide/dream.md): dreams write the palace, recall reads it cheaply.
- **Warnings section** for configuration mismatches: a reasoning effort or token budget the selected model does not support, a tool that is unavailable, a model that is not offered. (Today the reasoning selector is disabled with an explanation when unsupported; there is no general warnings area.)

- **Hand a web chat over to Telegram**: a voluntary action that sets the chat's `origin` to `telegram` (the marker in `<chat_id>.origin.json`, already exposed in chat summaries) and notifies the user, so the bot continues it. The origin marker is the base; nothing else exists yet.
- **Orchestrator follow-ups**: a default routed agent for the web UI, per-agent routing hints beyond the description, and a log of routing decisions to tune descriptions.

### tools

- **RECALL knowledge, phase 2** (the admin pages `admin-cli`, `recipes` and `security-for-admins` are done): reference pages for Telegram, models and providers, MCP, more recipes (step-by-step fixes) and the user documentation, added to `src/knowledge/recall/` ([RECALL knowledge](../user-guide/recall-knowledge.md) is phase 1).
- **Read access to docs and source for agents**: let an agent read `docs/` and the code to answer deeper questions. `docs/` is not in the Docker image today; remove it from `.dockerignore` first.
- **Agent-proposed config edits with approval cards**: an agent proposes a change to its own yaml (a tool group, a limit) and the user approves or rejects it in a card, like terminal approvals, instead of editing files by hand.
- **File tools v2** for every source: paginated reads with line numbers and text extraction for PDF/Office files; regex content search and glob file search; `patch` (unique find-and-replace with tolerant matching, returning a diff); a stale-write guard.
- **Terminal follow-ups** (phase 1 is [done](#done)): a background `process` tool (start, poll, wait, kill); LLM-assisted smart approvals (a small model classifies borderline commands instead of the pattern lists); a per-chat "always allow" for a command or pattern; a Docker backend that creates a per-agent sandbox container itself.
- **Terminal multi-backend follow-ups**: an agent may use several backends (sandbox, the user's own PC over Windows OpenSSH); still open: a Windows-native backend without Git Bash, a per-chat "always allow" per backend, wake-on-LAN/availability check before a PC call.
- **Approval rules for folders and MCP**: route destructive folder operations (`folder_delete`, `folder_move`, memory delete/move on user folders) and non-read-only MCP tools through the [approval rules](../user-guide/terminal.md#approval-rules) with the tool kinds `folder_write` and `mcp_call` (default: folder writes allow except delete/move ask, MCP read-only allow, other MCP ask). Today they never ask. Needs the engine called from the folder and MCP handlers (they have no terminal session), approval cards for actions with no command or backend, and a decision for unattended contexts (Telegram, CLI) that now would deny. The engine's `Action` and `evaluate` are ready for it.
- **Approvals form: inherit the layer defaults**: the Agents tab always writes the per-backend `approvals`, which pins the shorthand and hides the `default` of the user and installation rule lists; add an "inherit" choice.
- **Masked secret-input card**: an agent asks for a credential with a card in the web chat whose field is masked and whose value goes straight to `.config/secrets.env` (like `python -m src.admin set-secret`), never through the chat or the model; today the user runs `set-secret` in a terminal.
- **Per-user path limits for the local backend** (the setup guide): confine an agent's local commands and file tools to its own user's `.config/<user>/` and `data/<user>/`, so a guide for one user cannot read another's data; today only rules and approvals protect other users.
- **Stronger isolation for the local terminal backend**: protected paths ([done](../user-guide/terminal.md#protected-paths-local-backend)) refuse SSH keys, `secrets.env`, `.env` files, login credentials and `/proc/*/environ` for file tools exactly and for commands by scanning, but the command scan is best effort (obfuscated paths, hard links and agent-written programs bypass it) and the RECALL user can still read everything else (`user.yaml`, other users' `data/`). Options, strongest first: run the guide's commands as a separate OS user (a `sudo -u recall-guide` wrapper or `su`) that cannot read `.config/keys`, `secrets.env` or other users' folders; run them in a separate container that mounts only what the guide needs (`python -m src.admin` over a socket or a narrow volume); make the key files readable only by a different user than the one the agent shell runs as and have the SSH backend use a small helper; extend the refused list to other users' `data/` once per-user path limits exist.
- **Server admin agent** for the home Proxmox server, built on the terminal feature: Proxmox API through an MCP server, SSH as a dedicated sudo user, read-only commands free and state changes approved by the user, its own agent only for the owner, an audit log, read-only dreams for health review. Risk: AdGuard on that server is the DHCP/DNS of the whole home network.
- **Image and graph generation** (possibly through an MCP server). PDF creation is [done](#done) (`make_pdf`); still open for PDFs: designed documents with covers and photos, form filling and OCR of scans on the way in.
- **Remote MCP servers** (streamable HTTP); Node.js and the Google Calendar sign-in in the Docker image ([MCP](../user-guide/mcp.md#runtime-behavior)).

### web and tests

- **Chat loading, next steps**: gzip for the JSON answers; keep files the agent sent (`media[].data_url`, base64) out of the chat payload and serve them by URL (a 28-message chat weighs 1.3 MB, 870 KB of it files); fewer requests per chat switch (`/api/chats`, approvals, tasks and processes are still separate calls, and the chat list is also polled); lighter bubbles for tool steps and reasoning, since the cost left in a long chat is the number of DOM nodes.
- **Node tests**: `tests/test_model_sorting.cjs` fails on a clean checkout (its page stub lacks `matchMedia`, `window`, `MutationObserver`, `location` and more, each one added reveals the next; a generic stub or a real DOM such as jsdom would end that), and the Python suite does not run the `.cjs` files, so failures there go unnoticed; wire them into the check run before a release.
- **PDF follow-ups**: designed documents (covers, photos) as a skill, a way to merge, split or fill PDF forms, an end-to-end check with a real model (does `make_pdf` look good, does a model receive the `view_image` picture; Ollama is unchecked), and `view_image` for formats Pillow cannot read.

### usage and data

- **Cost control**: spend per user, agent and day (building on `src/agent/usage.py`), shown in the web UI, optional monthly caps and a separate cap for dreams.
- **Backup / export and import of an agent** (`SOUL.md`, memories, skills, palace, conversations); scheduled backups of `data/` for Docker.
- **Obsidian safe/auto overlay** with optional auto-commit, designed in [overlay](../architecture/obsidian-overlay.md); today a user folder pointing at the vault is the implemented way.
- Tags in the vector index.

### interface

- Search across chat history from the web UI (the agent can already search its conversations; the user cannot).
- Optional voice replies (TTS) in Telegram, per agent.
- Remove unnecessary messages from the settings and configuration selectors to keep the interface clean.
- A global `defaults.yaml` merged under `user.yaml` and agent files, and a channel to user index in a `ConfigStore` ([agent.yaml](../reference/agent-yaml.md#loading)).

### permission level follow-ups

Not built yet, in the order they matter (see the [known gaps](../architecture/permissions.md#known-gaps-out-of-scope-for-the-level-work)):

- **Outbound network control**: a domain allowlist for `web`/`web_retrieve` and egress rules for the terminal sandbox; the largest remaining exfiltration channel.
- **Dream extras**: the limited read-only delegate (configurable, off by default), MCP read-only tools and `sandbox_read`/`sandbox_search` on disposable sandboxes.
- **Folder and MCP actions through the rule engine** (deletes, moves, non-read-only MCP tools), with approval cards that do not assume a command.
- **Per-user path isolation**, **session revocation on demotion**, **login throttle** and a **CSRF token**.
- **Audit of every write** (files, memory, self), not only terminal and permission changes; **budgets** per agent.
- **Outbound messages to third parties** as their own service, never below operator.

## From the replica report (2026-11-05)

Capability gaps an agent reported, in the order they block it; each line says what depends on it.

1. **Scheduled jobs and proactive Telegram messages**: cron-like jobs with an unattended approval policy; reminders, recurring reports and any automation depend on it.
2. **Ask the user with options**: clarify cards in web and Telegram; confirming choices without free-text round trips depends on it.
3. **Browser**: driving a web page; sites that need clicks, logins or scripts depend on it.
4. **Vision**: analysing images the agent did not receive in chat; screenshots and files on the backend depend on it.
5. **Voice tools**: speech output and input as tools; spoken replies depend on it.
6. **Connectors**: mail, calendar, drive and similar accounts beyond MCP; personal-data tasks depend on it.

Skills imported from other agent systems:

- **Per-skill platform/compatibility tag**: mark which platform a skill is written for, so an agent knows whether its commands and tools exist here.
- **Agents should not see foreign frontmatter**: strip or ignore foreign frontmatter keys in imported skills so they do not confuse the model.

## done

- Slow chats and duplicated streaming on reload (bugs reported 2026-11-14): a reply that is still running no longer shows its saved checkpoint next to the live stream (it was drawn twice and marked interrupted); the chat list keeps what it read from each transcript until the file changes instead of parsing all of them on every request; opening a chat asks for the preferences and the chat together and no longer repeats the models request; a long chat opens with its last 60 messages and a bar to load earlier ones (`GET .../chats/{id}?limit=&before=`, `window` in the answer). Measured with Chromium on 29 real chats over a simulated Tailscale link (80 ms, 40 Mbit/s): median switch 334 ms to 147 ms, p90 599 ms to 397 ms. Still open: gzip, keeping sent files out of the chat payload, and fewer requests per switch (`/api/chats`, approvals, tasks and processes are still separate calls).
- `view_image` (group `memory_read`): the agent looks at an image or at pages of a PDF (a source or a terminal backend, one page or a contact sheet of up to 6), downscaled to a JPEG and shown as the next message, removed from the context at the user's next message, 6 per message, only for models that declare image input. `send_file` takes `<backend>:<path>` (fetched through the terminal session, with its approval and audit).
- `make_pdf` (group `memory_edit`): the agent describes a document as a list of elements (heading, paragraph, bullets, numbered, table, image, spacer, pagebreak) and RECALL lays it out with reportlab and saves it in any source the agent may write (default `mind_palace:documents/`), without sending it. `read` and `folder_read` return a PDF's text, and `move` and `folder_move` now copy bytes, so PDFs and images move intact ([files and media](../user-guide/files-and-media.md#pdf-files-made-by-the-agent), [tools](../reference/tools.md#memory_edit)).
- Agent-sent files and notices: `send_file` (group `files_send`, level `reader`) hands the user a file the agent can already read, in the web chat and on Telegram; `notify_telegram` (group `notify`, level `assistant`, 30 an hour) sends the owner a short Telegram message ([files and media](../user-guide/files-and-media.md#files-and-notices-from-the-agent), [tools](../reference/tools.md#files_send)). `send_file` also takes `<backend>:<path>` (a file on a terminal machine, through its approval), and `view_image` (group `memory_read`) lets a model that accepts images look at an image or at PDF pages, see the `make_pdf` entry below.
- A reloaded chat gives the model its tool calls and results back, not only the text ([memory](../user-guide/memory.md#context-compaction)); a reply cut by a restart is kept as a checkpoint ([API](../reference/api.md#checkpointed-replies)).
- `/compact` works on short chats: it keeps the latest 20 % and says so ([memory](../user-guide/memory.md#context-compaction)).
- Web chat: playable audio and video and image previews of what you send, permanent transcriptions under each recording, a voice recording button, paste with Ctrl+V, a background-process panel, and a dot in the chat list while an agent works or a reply is unread (kept on the server).
- Search inside conversations (`session_search`, meaning-based or exact text; [tools reference](../reference/tools.md)).
- Subagents (`delegate` group: `task_start` and task tools, budgets and limits, backend rules), messages between agents (`task_start(agent=...)` hands a task to another agent of the same user, allowlisted in `delegation.targets`) and background processes (`process_*` tools) ([delegation](../user-guide/delegation.md), [terminal guide](../user-guide/terminal.md#background-processes)).
- Terminal tool group, phase 1: `terminal`, `sandbox_read`, `sandbox_write`, `sandbox_patch` and `sandbox_search` against an SSH sandbox (or a local backend on trusted installs), foreground commands with timeouts, approvals (manual / smart / off) through the API and Telegram buttons, a hardline blocklist, redacted and truncated output, an audit file, unattended behaviour for dreams, and `python -m src.admin terminal-test` ([terminal guide](../user-guide/terminal.md)).
- Telegram orchestrator: a decision model (OpenRouter Decisions API, alpha) picks the agent for a message that starts a new chat, with override buttons ([Telegram](../user-guide/telegram.md#telegram-orchestrator-automatic-agent-choice)).
- Video input: models with image input but no video get sampled frames plus transcribed audio ([files and media](../user-guide/files-and-media.md#video-input)).
- Message timestamps and `[AUDIO]` / `[VIDEO]` tags in the model context ([files and media](../user-guide/files-and-media.md#message-tags-and-timestamps)).
- PDF reading with Tesseract OCR for scanned pages ([files and media](../user-guide/files-and-media.md#pdf-input)).
- Date filters and other filters for web search; tool groups per agent ([tools](../reference/tools.md)).
- Google Calendar through MCP ([MCP servers](../user-guide/mcp.md)).
- Prompt caching for Claude on OpenRouter with cache usage in the debug panel ([settings.yaml](../reference/settings-yaml.md#prompt-caching)).
- Zero data retention as an optional user policy, `require_zdr` ([models and providers](../user-guide/models-and-providers.md#openrouter-model-catalog-and-zero-data-retention)).
- Claude subscription provider, experimental, with per-user logins through `claude_subscription.claude_config_dir` ([Claude subscription](../user-guide/claude-subscription.md)).
- Provider tabs in the model picker including **Any**; per-agent default model `chat_config.model`; favorites.
- Dream mode ([dream mode](../user-guide/dream.md)); associative memory v2 with user folders as sources, incremental indexing and score threshold 0.35, and a per-user maximum number of matches ([memory](../user-guide/memory.md)).
- Associative memory injected while the model already generates, with a latency breakdown ([architecture](../architecture/overview.md#associative-recall-in-parallel)).
- Web: agents tab, new/manage/delete chats, parallel and steerable generation, automatic and manual chat names, password login (no registration), styled error cards with codes and an empty state for users without agents ([API reference](../reference/api.md#error-codes)).
- Telegram channel with several users mapped through `TELEGRAM_ALLOWED_USERS=<telegram_id>=<user_id>` ([Telegram](../user-guide/telegram.md)).
- Docker image, Compose file and Watchtower ([Docker](../deployment/docker.md)).
- Friendlier handling of provider 429 errors with a model hint.
