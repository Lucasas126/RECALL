# Features

## Multimodal capabilities

- ~~Allow all models with image support to process video. If a model does not natively accept video directly, split the video into frames and audio. Send the audio to STT, send the frames separately, and inform the model that they come from a decomposed video.~~
- ~~Include message timestamps in conversations so models know the relevant dates. Include [AUDIO] and [VIDEO] tags in the conversation to indicate when a message contains or was generated from audio or video content. This will help models understand the context of the conversation and provide more accurate responses.~~
- pdf generation?
- image and graph generation with an mcp?
- ~~pdf reading and extraction~~ (text from selectable PDF text, with Tesseract OCR for scanned pages; see [config.md](config.md#pdf-input))

## Search and integrations

- ~~Add date filters to web search tools.~~
- ~~Separate and group all tools: web search, obsidian read only, obsidian read+edit, etc. All groups should be exposed and available to select for all agents, and should be configured in the agent tab in the web interface. This will allow users to easily manage and configure tool access for different agents, so not all agents have the same tools.~~
- ~~Add Google Calendar integration.~~ (through MCP, see [mcp.md](mcp.md))
- ~~prompt caching for Claude on OpenRouter, with cache usage in the debug panel~~ (see [config.md](config.md#prompt-caching))
- allow claude suscription to be used, similarly to how its implemented in other agent frameworks with and sdk

## Automation and behavior

- Add dream mode (background self-review of conversations; see [dream.md](dream.md)).
- Reminders and proactive messages: the agent can schedule a message for the user ("remind me on Friday...") and send it at that time through Telegram (and show it in the web). Reuse the dream scheduler. First realistic step towards automations.
- Add automations with rules and actions based on conversation or calendar events.
- Recall mode (core idea from the README, not implemented): the agent enters its mind palace with a context trimmed to the last messages, searches and reads what it needs, and returns only the relevant information to the main conversation, dropping the whole retrieval process from its context. Complements dream mode: dreams write the palace, recall reads it cheaply.
- Chat compaction: instead of dropping the oldest messages beyond `conversation_messages_retained`, summarize them into a running summary kept at the start of the context, so long chats do not "forget" mid-conversation.

## Tools

- File tools v2 for every source (`mind_palace:`, `conversations:`, `vault:`), like other agent frameworks' file tools:
    - paginated reads with line numbers (`offset`/`limit`, a character cap per read) and text extraction for PDF/Office files;
    - regex content search (grep-like, with context lines) and file search by name/glob;
    - `patch`: find-and-replace of a unique text span with tolerant matching strategies (exact, trimmed lines, normalized whitespace, flexible indentation...), returning a unified diff instead of rewriting whole files;
    - stale-write guard: refuse to overwrite a file the agent has not read in this task, or that changed on disk since it read it.
- Terminal tool group (shell access like other agent frameworks), off by default and enabled per agent; design separately because of the risk in a multi-user platform:
    - backends: a per-agent Docker sandbox container with only the agent's workspace mounted (default on the server; never inside the RECALL container, which holds `.env` and every user's data) and a local backend (the host machine) only for trusted single-user installs;
    - foreground commands with a timeout and truncated output (full output saved to a file); background processes for long jobs with a companion tool to poll/wait/kill and a notification when they finish;
    - approvals: classify dangerous commands with patterns (recursive deletes, killing processes, writing `.env`/credentials/config, ...) and ask the user through the web UI or Telegram inline buttons (approve / deny), with modes manual / smart (small model decides) / off;
    - child processes get an environment without provider API keys, and error output is scrubbed of secrets;
    - not available in dream mode (or read-only commands only).
    - an SSH backend (run commands on a remote host with a per-agent private key) besides the Docker sandbox and local backends; needed by the server admin agent below.
- Server admin agent ("sysadmin") for the home Proxmox server, built on the terminal feature. RECALL runs in an unprivileged LXC that cannot see the host (by design), so the agent manages it from outside:
    - Proxmox API through an existing Proxmox MCP server (e.g. ProxmoxMCP-Plus) with an API token: list and monitor CTs/VMs and resources, start/stop/restart, snapshots, backups, tasks and logs;
    - SSH to the host as a dedicated `recall-admin` user with `sudo` (not root login), so it can read anything (`journalctl`, `zpool status`, `/etc`, `pct exec <ct> -- ...` inside containers) and privileges can be narrowed later in sudoers;
    - policy: read-only commands run freely; anything that changes state (writes, service restarts, package installs, `pct`/`qm` actions, deletes) needs the user's approval through Telegram inline buttons or the web UI; content read from outside (web pages, files) can never bypass approval;
    - its own agent, only for the owner user, with web access off or always behind approval; the SSH key and API token are secrets only this agent can use;
    - audit log of every command and API action (with result), visible in the web UI;
    - dream sessions for it review server health read-only (disks/ZFS, backups, stopped containers, pending updates, AdGuard/DHCP up) and notify the user; never change anything during a dream;
    - risk to keep in mind: the server runs AdGuard as the DHCP/DNS of the whole home network, so a wrong action can take the house offline.

## Usage and data

- Cost control: spend per user, agent and day (tokens and money, building on `src/agent/usage.py`), shown in the web UI, with optional monthly caps per user/agent and a separate cap for dream sessions.
- Backup / export of an agent: archive with `SOUL.md`, memories, skills, mind palace and conversations, plus import; scheduled backups of `data/` for the Docker deployment.
- Multi-source vector index (README idea): index the bound vault / user folders besides the mind palace, with the existing per-agent isolation.
- Obsidian safe/auto overlay and optional auto-commit (designed in [overlay.md](overlay.md), not implemented): the next layer on top of the simple `vault:` folder source.

## Configuration and interface

- Remove unnecessary messages from the settings selection and other configuration options to keep the interface clean.
- Warnings section for configuration mismatches (see [pending.md](pending.md)): unsupported reasoning effort or token budget for the selected model, unavailable tools, unsupported models.
- Search across chat history from the web UI (the agent can already search conversations; the user cannot).
- Optional voice replies (TTS) in Telegram, per agent.
- ~~Web interface tab where an user can handle, create and edit agents, with a list of all the agents the user has access to, and the ability to create new agents or edit existing ones: changing the system prompt, and all confiurations that are now in the agent json file~~

## Chat UI

~~- Option to create new chats: context is empty, except for the current agent system prompt.~~
~~- Option to choose from the different models the user has access to.~~
~~- Option to view and manage existing chats: users can see a list of their previous conversations, with the ability to delete or continue them.~~
- ~~small llm that generates a name of the conversation to display on the chat list, its runs when a conversation is at least 5 messages long, and the name is stored in the conversation metadata. if the conversation is less than 5 messages long, keep the current method. the small llm must be a general settings, not from teh user or the chat, but in the general settings~~
- ~~its possible to swap chat while another one is working, so you are not forced to stay in the same chat while the model is generating a response~~
- ~~chats using the same agent can generate in parallel without sharing conversation context~~
- ~~chat names can be changed and edited~~
- ~~user authentication. allowed users must be defined beforehand in a configuration file, there is no user registration system, only password authentication~~

## Recall
- ~~make it so associaive memory is inyected into the system prompt while the model has started generating text, so its faster and more efficient. This will allow the model to have access to relevant information without having to wait for the entire context to be processed before generating a response. Make it so the model is aware that this can happen, so it can adjust its behavior accordingly. This will improve latency. Since the process will be parallel, we have to adjust the timings so it makes sense (right now since they are not parallel, they are just summed in the latency metric that we print in the debug panel in the web ui)~~

## Errors
- ~~[error] openrouter API error 429: Provider returned error, happened when trying to use cheapeast model in openrouter, probably because its not zdr or something like that, try to handle it better and show a more user friendly message, such as a suggestion to changing the model~~ (fixed: upstream detail and a model hint are shown)
