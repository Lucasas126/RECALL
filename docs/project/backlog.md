# backlog

One list of ideas and planned work. Implemented behaviour is documented in the
[user guide](../README.md); nothing under **planned** exists in the code yet (checked against the code when this page was written).

## planned

- **Secrets for admin agents** are done: the opt-in `secrets` group (admin only) lists names and copies, renames or deletes variables without ever seeing a value, one approval card each. Setting a new value stays with the user (`set-secret` or Settings, Providers).

### resilience

- **Level and tool groups in a running chat**: approval rules are re-read before every decision and apply at once, but a changed level, tool group or tool availability still applies only to new chats (the chat keeps the tools it was built with). Open: whether to rebuild the tools between turns of a running chat.

### automation and behavior

- **Agents shared between users** (idea): one agent that several users can open, with a memory (mind palace) shared by all of them, for example a household agent for two people. Users could also talk to each other through it, with the agent relaying or summarising. Open: who owns the agent and its files, per-user chats versus one shared chat, what each user may read of the other's messages, permission level and approvals (whose limit applies), and where the shared memory lives so private data of one user never leaks to the other.

- **Automations** are being built in steps: the service (triggers `at`, `every`, `cron`, `process_end`; guards; catch-up) is done, see [automations](../user-guide/automations.md); the agent tools (group `automations`, level `assistant`) and the web tab (a second tab of the Tasks dialog) are done too. Left for later: sending a result to a chat. Calendar and conversation events are out of scope.
- **Reminders and proactive messages**: a one-off message the agent sends while it works exists (`notify_telegram`) and an agent can schedule work for later with the `automations` tools (see above). Still open: a scheduled message delivered straight to Telegram and shown in the web without running the agent ("sending a result to a chat" in the automations item).
- **Recall mode**: the agent enters its mind palace with a context trimmed to the last messages, searches and reads what it needs, and returns only the relevant information, dropping the retrieval process from its context. Complements [dream mode](../user-guide/dream.md): dreams write the palace, recall reads it cheaply.
- **Warnings section** for configuration mismatches: a reasoning effort or token budget the selected model does not support, a tool that is unavailable, a model that is not offered. (Today the reasoning selector is disabled with an explanation when unsupported; there is no general warnings area.)

- **Hand a web chat over to Telegram**: a voluntary action that sets the chat's `origin` to `telegram` (the marker in `<chat_id>.origin.json`, already exposed in chat summaries) and notifies the user, so the bot continues it. The origin marker is the base; nothing else exists yet.
- **Orchestrator follow-ups**: a default routed agent for the web UI, per-agent routing hints beyond the description, and a log of routing decisions to tune descriptions.

### interface

- **Admin web interface**, visible only to administrators:
  - *First run*: when no user exists, the web UI opens a setup page that creates the first (admin) user, its password, provider and key variable name, instead of `create-user` and hand-edited YAML. It must be impossible to reach once a user exists.
  - *User management*: list, create, edit (name, language, provider, `max_level`) and delete users, reset a password and see each user's agents and levels; the same rules as the admin CLI (`max_level` only set by an administrator, refusals named the same way).
  - Needs a notion of *administrator* in the user model (today only the CLI and the host are administrators), audit of every change, and no access to other users' chats or secrets from this page.
- **Per-agent accent colour** (optional): today the accent colour and theme are one browser-wide preference (`localStorage`), so every agent looks the same apart from its picture. Let an agent's file set an optional accent (one of the swatches or a custom colour) that the web UI applies while that agent is open, falling back to the user's own choice; the agent list and the Telegram bot are unaffected.

### tools

- **RECALL knowledge, phase 2** (the admin pages `admin-cli`, `recipes` and `security-for-admins` are done): reference pages for Telegram, models and providers, MCP, more recipes (step-by-step fixes) and the user documentation, added to `src/knowledge/recall/` ([RECALL knowledge](../user-guide/recall-knowledge.md) is phase 1).
- **Read access to docs and source for agents**: let an agent read `docs/` and the code to answer deeper questions. `docs/` is not in the Docker image today; remove it from `.dockerignore` first.
- **File tools v2** for every source: paginated reads with line numbers and text extraction for PDF/Office files; regex content search and glob file search; `patch` (unique find-and-replace with tolerant matching, returning a diff); a stale-write guard.
- **Terminal follow-ups**: LLM-assisted smart approvals (a small model classifies borderline commands instead of the pattern lists); a per-chat "always allow" for a command or pattern; a Docker backend that creates a per-agent sandbox container itself.
- **Terminal multi-backend follow-ups**: an agent may use several backends (sandbox, a Windows machine over OpenSSH); still open: a Windows-native backend without Git Bash, a per-chat "always allow" per backend, wake-on-LAN/availability check before a PC call.
- **Approval rules for folders and MCP**: route destructive folder operations (`folder_delete`, `folder_move`, memory delete/move on user folders) and non-read-only MCP tools through the [approval rules](../user-guide/terminal.md#approval-rules) with the tool kinds `folder_write` and `mcp_call` (default: folder writes allow except delete/move ask, MCP read-only allow, other MCP ask). Today they never ask. Needs the engine called from the folder and MCP handlers (they have no terminal session), approval cards for actions with no command or backend, and a decision for unattended contexts (Telegram, CLI) that now would deny. The engine's `Action` and `evaluate` are ready for it.
- **Approvals form: inherit the layer defaults**: the Agents tab always writes the per-backend `approvals`, which pins the shorthand and hides the `default` of the user and installation rule lists; add an "inherit" choice.
- **Masked secret-input card**: an agent asks for a credential with a card in the web chat whose field is masked and whose value goes straight to `.config/secrets.env` (like `python -m src.admin set-secret`), never through the chat or the model; today the user runs `set-secret` in a terminal.
- **Per-user path limits for the local backend** (the setup guide): confine an agent's local commands and file tools to its own user's `.config/<user>/` and `data/<user>/`, so a guide for one user cannot read another's data; today only rules and approvals protect other users.
- **Stronger isolation for the local terminal backend**: protected paths ([done](../user-guide/terminal.md#protected-paths-local-backend)) refuse SSH keys, `secrets.env`, `.env` files, login credentials and `/proc/*/environ` for file tools exactly and for commands by scanning, but the command scan is best effort (obfuscated paths, hard links and agent-written programs bypass it) and the RECALL user can still read everything else (`user.yaml`, other users' `data/`). Options, strongest first: run the guide's commands as a separate OS user (a `sudo -u recall-guide` wrapper or `su`) that cannot read `.config/keys`, `secrets.env` or other users' folders; run them in a separate container that mounts only what the guide needs (`python -m src.admin` over a socket or a narrow volume); make the key files readable only by a different user than the one the agent shell runs as and have the SSH backend use a small helper; extend the refused list to other users' `data/` once per-user path limits exist.
- **Server admin agent** for a home server, built on the terminal feature: Proxmox API through an MCP server, SSH as a dedicated sudo user, read-only commands free and state changes approved by the user, its own agent only for the owner, an audit log, read-only dreams for health review. Risk: a mistake on a server that also runs the network's DHCP/DNS affects the whole network.
- **Image and graph generation** (possibly through an MCP server). PDF creation is done (`make_pdf`); still open for PDFs: designed documents with covers and photos, form filling and OCR of scans on the way in.
- **Remote MCP servers with OAuth** (streamable HTTP with a per-user bearer token is built; an OAuth sign-in for remote servers is not) and the Google Calendar sign-in in the Docker image (Node.js is already in the image for the Claude CLI, `npx` is left out on purpose) ([MCP](../user-guide/mcp.md#runtime-behavior)).

### web and tests

- **Chat loading, next steps**: gzip for the JSON answers; keep files the agent sent (`media[].data_url`, base64) out of the chat payload and serve them by URL (a 28-message chat weighs 1.3 MB, 870 KB of it files); fewer requests per chat switch (`/api/chats`, approvals, tasks and processes are still separate calls, and the chat list is also polled); lighter bubbles for tool steps and reasoning, since the cost left in a long chat is the number of DOM nodes.
- **Node tests in CI**: `tests/web/js/test_node_suite.py` now runs every `tests/web/js/*.cjs` inside the Python suite (all green), but the GitHub workflow only builds the image and runs no tests; add a test job that runs the suite before a release.
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

### maintenance and workflow

- **Releases**: a `scripts/release.sh X.Y.Z` (bump `pyproject.toml` and `uv.lock`, check the tag does not exist, draft notes from `git log`) and a CI check that the two versions match. Today `docker-publish.yml` only compares the release tag with `pyproject.toml`; releases are done by hand.
- **Seamless restarts**: resume a generation cut by a restart, detached ssh processes, graceful stop (nothing is built yet; background processes and tasks are described in the [delegation guide](../user-guide/delegation.md)).
- **Approval fatigue**: built-in read-only allow rules for remote backends (today the fallback is always "ask" and rules come only from the user, agent and installation layers).
- **Web slash commands**: accept `/dreamstart` and `/dreamcancel` (Telegram-only aliases today; the web parser in `app.js` only knows `/dream start`).
- **`prune_empty`** deletes any empty chat that no client protects with `keep`. Telegram answers in a new chat since 0.3.18 and the web does the same since 0.4.0 (`chat_replaced` event, only for generated chat ids and not for edits), so the message is no longer lost. The empty chat still disappears on its own; a grace period for young empty chats was rejected on purpose (no stray empty chats). The page keeps the draft, recording and attachments when its empty chat vanishes, and sending still works through `chat_replaced`.
- **Mobile**: the Dream sessions table scrolls sideways, so the "Duration" column is cut; the Dream and Folders tabs have not been reviewed on a phone.
- **Tests**: review of the suite (about 2343 tests, about 185 s: cut redundant ones). No failing test and no skip on Linux as of 0.4.0; the OCR test needs `tesseract` on the machine that runs the suite (the Docker image has it).
- **Tool ergonomics**: `memory` could infer `patch` when `old_text` is given without `action` (it errors with "give an action"); an address without a colon (`mind_palace/x.md`) could suggest `mind_palace:x.md` in the error.
- **Version in the prompt**: the system prompt tells the agent when its profile was loaded but not the running version (`/api/version` exists, no agent tool reads it).

### permission level follow-ups

Not built yet, in the order they matter (see the [known gaps](../architecture/permissions.md#known-gaps-out-of-scope-for-the-level-work)):

- **Outbound network control**: a domain allowlist for `web`/`web_retrieve` and egress rules for the terminal sandbox; the largest remaining exfiltration channel.
- **Dream extras**: MCP read-only tools and `sandbox_read`/`sandbox_search` on disposable sandboxes. The limited read-only delegate is done (`dream.delegate`, off by default; [dream](../user-guide/dream.md)).
- **Folder and MCP actions through the rule engine** (deletes, moves, non-read-only MCP tools), with approval cards that do not assume a command.
- **Per-user path isolation**, **session revocation on demotion** (today a lowered `max_level` applies on the next request but the session lives on), **login throttle** (`/api/login` has none) and a **CSRF token** (only the `SameSite=strict` cookie today).
- **Audit of every write** (files, memory, self), not only terminal commands and approval-rule changes; **budgets** per agent.
- **Outbound messages to third parties** as their own service, never below operator.

## Smaller improvements

Capability gaps, in the order they block use; each line says what depends on it.

1. **Scheduled jobs and proactive Telegram messages**: done as [automations](../user-guide/automations.md) (`at`, `every`, `cron`, `process_end`; unattended runs) plus `notify_telegram`. Left: sending an automation's result to a chat.
2. **Ask the user with options**: clarify cards in web and Telegram; confirming choices without free-text round trips depends on it.
3. **Browser**: driving a web page; sites that need clicks, logins or scripts depend on it.
4. **Vision**: done as `view_image` (a source or a terminal backend file, or PDF pages). Left: providers without image input (OpenRouter/Ollama cases).
5. **Voice tools**: speech output and input as tools; spoken replies depend on it.
6. **Connectors**: mail, calendar, drive and similar accounts beyond MCP; personal-data tasks depend on it.

Skills imported from other agent systems:

- **Per-skill platform/compatibility tag**: mark which platform a skill is written for, so an agent knows whether its commands and tools exist here.
- **Agents should not see foreign frontmatter keys**: strip or ignore them in imported skills so they do not confuse the model.


## done

- **@mention another agent in a chat** (0.5.0, 0.5.1): a message starting with `@<agent_id>` is answered by that agent with its own permissions, model and memory; it gets a capped, untrusted excerpt of the chat; the reply shows with its name and picture from the first moment, is kept in both chats, can be stopped, and its approvals are decided as its own (also on Telegram). The message box lists agents on `@` and slash commands on `/`. Open: whether automations or other agents should be able to mention.
- **Web UI fixes** (0.5.0): switching agent in the top selector opens a fresh chat and rolls back cleanly if that fails (not yet tried by hand); a voice message transcript shows once, live, without the raw `[AUDIO]` block; the usage disclosure stays available while the agent replies; the subagent tray always shows the model.

Implemented features are listed by topic in the [changelog](changelog.md).
