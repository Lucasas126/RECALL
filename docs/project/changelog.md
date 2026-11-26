# changelog

The notes of each release are on the GitHub Releases page. This file groups what changed by topic, starting with 0.4.0, which
sums up everything since 0.2.0 (5 November 2026): 0.2.x and 0.3.x, 34 releases between 5 and 15 November.

## Unreleased

- Shared agents: several users can use one agent (same SOUL, memory, chats, tools; level = lowest member's; the creator's keys and Claude subscription pay). Share from the agent settings by user id; the invited person accepts or declines in the Agents tab. Messages carry the speaker's name and `USER.md` has one section per member. Leave, or unshare (copies for everyone, automations paused). New `space` and `agent share|unshare` admin commands and `/api/invitations` routes. Notifications to every member and Telegram per speaker are still in progress. See [shared agents](../user-guide/shared-agents.md).

## 0.6.2 (26 November 2026)

- Web UI: the one-line message prompt now lines up with the text caret in the narrow composer.

## 0.6.1 (26 November 2026)

- Web UI: browser notifications now show the agent name in the first line ("<agent> · Reply ready"); the chat title stays below.
- Web UI: on narrow screens the model and reasoning controls move out of their own row into a panel opened by one options button next to the attach button; wide layouts are unchanged.
- Web UI: a long agent name in the message prompt stays on one line; if it does not fit it scrolls slowly in a loop (only an ellipsis with reduced motion).

## 0.6.0 (26 November 2026)

- Web UI: optional in-browser notifications (a bell in the side panel footer, per device, off by default; no Web Push). The page itself shows "Reply ready", "Approval needed" and "Automation finished" with the chat or automation title, only while it is in the background; clicking opens the chat or the Automations dialog. New `GET /api/automations/events` lists runs that ended since a marker (ids, title and status only). Telegram notifications of automations are unchanged. Removed the Tasks tab of the former Tasks dialog (a list of background tasks nobody used); the dialog is now **Automations** only.
- Web UI: a new version is announced by a small in-page banner ("A new version of RECALL is available", Reload and dismiss) instead of being applied silently. The service worker no longer calls `skipWaiting` on install; Reload posts `SKIP_WAITING` and reloads on `controllerchange`. The page checks for a new worker on focus (at most every 5 minutes). No notifications or push.
- Web UI: on screens up to 600 px wide the make-disposable button shows only its icon (44 px target, name kept in `aria-label` and `title`).
- Web UI: the theme and the accent colour are now user preferences kept on the server (`theme`, `accent`, `accent_color` in `/api/preferences`), so they follow you across devices. Each browser keeps a local copy so the first paint has no flash and works offline; on start the server's values win, and a browser's own values are uploaded once when the server has none. Saving is best effort and silent.
- Web UI: installable as an app (Windows, Android, iPhone). A service worker served at `/sw.js` keeps only the page shell for offline start-up (network first; never the API, event streams, uploads, downloads, media or cookie-setting answers) and logout clears its caches. The session cookie is now `SameSite=Lax` and lasts 30 days from the last activity (renewed at most once a day; it was strict and 14 days fixed). Voice recordings pick a supported type (Safari records `audio/mp4`) and name the file `.m4a`, `.ogg` or `.webm` after the real type. See [install the web UI as an app](../user-guide/settings.md#install-the-web-ui-as-an-app).
- Web UI voice messages: editing a message that has a recording now shows what was heard inside the one text box (correct it like any text), and the recording appears as a chip with its name and a cross to discard it, like a file waiting to be sent; the player stays after saving. The heard text is shown at the normal message size. While recording, the red button shows a small live level meter (8 bars) next to the elapsed time, and after 2.5 s without sound it dims, shows "!" and its tooltip asks to check the microphone. The edit request accepts `recordings` and `transcripts_in_text`.

## 0.5.5

- Web UI image viewer: Ctrl+wheel (and trackpad pinch) and the new zoom buttons or the `+`, `-` and `0` keys zoom only the image (1x to 8x, drag to pan, double click resets) instead of the whole page; when a message has several images, the left and right arrow keys and two side buttons step through them, with a counter. Zoom uses the bundled Panzoom library (MIT).
- New opt-in tool group `charts` (level `assistant`): `render_chart` draws Vega-Lite charts and `render_diagram` draws Graphviz diagrams as PNG files in a writable source, with a dark (default) or light theme and a bundled Open Sans font; no browser or network. `render_diagram` also draws Mermaid (flowchart, sequence, Gantt, Sankey) with a vendored Mermaid script in headless Chromium (new `playwright` dependency, no browser download). The Docker image now includes Graphviz and Chromium (`chromium-headless-shell`); installs outside Docker need Chromium for Mermaid.
- Safe parallel memory writes: every write tool (palace, folders, `memory`, `skill_manage`, `soul_edit`) holds the agent's lock for its whole read-modify-write (folders have their own lock, outside the folder), files are replaced atomically (temporary file, `fsync`, rename, permissions kept), and a whole-file write is refused when the file changed since the chat last read it (or was never read), instead of silently dropping the other change. As a result, `remember` subagent tasks are no longer refused while the target agent replies elsewhere or runs another remember task. See [concurrent writes](../architecture/sources.md#concurrent-writes).

- A subagent task may ask for up to 20 approvals per run (it was 10), so a worker that edits code under manual approvals can finish a normal task.
- Fix (web): the running-subagents tray and the task cards did not appear when a finished task was continued with `task_send` while the chat was open (the page stopped asking once every task was final); it now keeps a slow look (every ~12 s, visible tab only) next to the 3 s poll while something runs.
- A subagent task's worker chat can no longer be renamed or deleted through the chat endpoints (`PUT .../title`, `DELETE .../chats/<chat_id>`): they answer `400` "this is an internal task chat". It goes away with its task (retention pruning, or when the task is removed); a chat that must stay is created with an @mention.
- Hidden chats and hidden agents: the chat opened by an @mention is hidden from the chat list (the eye button at the right of the tabs lists it; still indexed and searched), and an agent can be marked `hidden` to leave the web selectors and be listed under **Hidden agents** (still reachable with `@`); every agent's editor gets **Open chat with this agent**.
- Web UI: the Worker chat dialog shows the name and picture of the agent that runs the worker on its replies, not those of the agent whose chat launched the task.

## 0.5.2

- Agent settings show the real application default (tool rounds per turn, delegation parallel tasks, time and tokens, read live from `settings.yaml`) instead of a vague text; reset empties those fields so they keep following the application.
- Web UI (desktop): the chat header shows the chat's name, editable in place (same rename as the side panel), and its height can be dragged taller (agent picture scales with it, remembered per browser). The phone header is unchanged.
- Agent settings, Tools tab: the tool, terminal and delegation selections come first; the approval rules, the learned approvals and the default verdict for unmatched actions follow in a "Terminal approvals" block where the two lists are collapsed by default with a count, and rule rows are more compact.
- The model picker is compact: providers are one scrollable row of chips (a dot marks an unavailable or experimental provider, details in the tooltip), search, favorites and refresh share one row, sort and the ZDR and capability filters sit in the collapsed Filters section, "Use ... default" is a small first entry, and each model is one line with badges and a price, with its details behind an info button. The dialog texts are translated to Spanish.
- Memory search is hybrid: keyword matches (BM25) are merged with the embedding matches, so exact names and model numbers are found. Existing indexes get their keyword table from the stored chunks, without new embedding calls.
- Associative memory injects up to 5 fragments by default (was 3), and 2 of them are reserved for the agent's own mind palace when relevant enough (similarity 0.40). Each user can change both in Settings; new `settings.yaml` keys `palace_slots`, `palace_threshold`, `palace_exclude`.

## 0.5.1

- Fix: the reply to an @mention shows the mentioned agent's name and picture from the first moment, instead of the chat's agent until its first words arrive.

## 0.5.0

Sums up the 0.4.x line (0.4.0 to 0.4.7, 16 to 19 November 2026) and adds what is new in this release.

### The 0.4.x line in short

- **Approval rules apply at once (0.4.3):** a rule added, removed, reset or denied is honoured by the next action in chats already running; level and tool groups still apply in new chats. One invalid agent file no longer breaks the web UI for its user, and a `user.yaml` whose id differs from its folder is refused with a clear error.
- **Remote MCP servers (0.4.4 to 0.4.6):** catalog servers can use streamable HTTP with a per-user access token pasted in Settings > Connections (checked, stored with mode 600, never shown again); `<config_dir>/mcp_servers.yaml` adds servers for one installation and survives image updates.
- **Agents can rename themselves (0.4.1):** the optional tool group `identity` with `set_display_name`, audited, no approval card.
- **Open source (0.4.2):** the repository was cleaned for publication and legacy config keys were removed (agent files that still carry `max_steps` or `memory_limits` must drop them).
- **Stop really stops (0.4.7):** Stop kills a running terminal command within about a second and aborts the associative-memory search; a draft survives when the server deletes an empty chat meanwhile.

### New in this release

- Message box: typing `/` at the start opens a list of the slash commands (`/dream`, `/compact`, `/usage`), and typing `@` at the start or after a space lists your other agents (with their picture). Up/Down move, Enter or Tab picks (Enter does not send while the list is open), Escape closes, a click picks; a command inserts `/name `, an agent `@agent_id `.
- New: @mention another agent in a chat. A message that starts with `@<agent_id>` of another of your agents is answered by that agent, with its own permissions, model and memory (the chat's agent does not run). It sees a short, untrusted excerpt of the chat. The reply shows with its name and picture, is kept in both chats (in its own one chat per originating chat) and can be stopped; its approvals are answered as its own. New `speaker` SSE event and `author_agent`/`author_name` on history messages.
- Fix: the usage line under the message box no longer vanishes while the agent replies, on send or on switching chats: it stays openable with the chat's last known report and updates when the reply finishes. It stays hidden in a new chat or one without a report, and never shows another chat's numbers.
- Fix: a voice message's transcript now shows once, as small text under the player, right after it is transcribed (it used to appear only after a reload) and no longer repeats as a raw "[AUDIO] Audio transcription:" block in the message text or the edit box. The model still receives the block. Editing a voice message keeps the recording and its transcript. New `transcript` SSE event.
- Fix: changing the agent in the top selector opens a fresh chat with it; if that cannot finish, the selector and the page go back to the previous agent and chat instead of disagreeing.
- Fix: the subagent tray above the message box shows each worker's model even when the row appeared before the model was known.

## 0.4.7

- Fix: an empty chat deleted by the server while you wrote or recorded in it (another tab, the phone) no longer costs you the draft: the page keeps the text, the recording and the attachments, and sending still works (the reply arrives in a new chat that the page follows).
- Fix: pressing Stop while an agent runs a terminal command now kills the command within about a second (it used to wait for the command or its timeout). The agent is told on its next turn that the action was stopped by the user and killed, the audit log marks it `cancelled`, and the web UI shows the step as *Stopping…* then *Stopped by you*, with a notice if the stop takes more than a few seconds.
- Fix: Stop also aborts the associative-memory search (its embedding request) instead of leaving it running.

## 0.4.6

- Fix: connecting an http MCP server with a token failed with "JSON decode error" (the web UI sent the body as an object instead of JSON). A test now checks that no call sends a raw object as body.

## 0.4.5

- Local MCP catalog: `<config_dir>/mcp_servers.yaml` (a `servers:` map, same format as `settings.yaml`) adds servers for one installation without editing the tracked `settings.yaml`, and survives image updates. Ids already in `settings.yaml` are refused.

## 0.4.4

- Remote MCP servers over streamable HTTP. A catalog server can be `transport: http` with a `url` (https, or plain http only to a private-network host) and `auth: {type: token}`. Each user pastes their own access token in Settings > Connections; RECALL checks it with the server before keeping it, stores it in the user's folder (mode 600) and never shows it again. If the server later rejects the token, tools ask the user to connect again. New route `PUT /api/mcp/<server_id>/token`. stdio servers work as before.

## 0.4.3

- Approval rules now apply at once in chats that are already running: a rule added, removed or reset (permission tools, web form, CLI or file edit) and a `deny` are honoured by the next action, not only in new chats. An unreadable agent or user file keeps the last good rules. The permission cards and tool results say what applies at once (approval rules) and what only applies in new chats (level, tool groups).
- One invalid agent file no longer breaks the web UI for its user: the **Agents** tab lists it as *Invalid configuration* with the reason, preferences load, and the other agents work. `agent list` marks it too.
- A `user.yaml` whose `id` differs from its folder is refused with an error naming both values; `agent check` reports it.

## 0.4.0

### Upgrading

- Every user needs `max_level` in `user.yaml` (0.3.0); `terminal.allow_in_dream` no longer exists.
- Remove `prompt_cache.trim_slack_percent` and `conversation_messages_retained` from a custom `settings.yaml`: compaction replaces them (0.3.7).
- `dream.max_steps` is gone; remove it from agent files (0.3.25).
- `questions.md` in an agent's palace is now `for_user.md` (0.3.8).
- The daily task token budget is removed and tasks have no token cap unless `max_tokens` is set (0.3.6).
- Sent audio and video are stored with the message and take disk space (0.3.11).
- The Docker image installs Tesseract (English and Spanish) for scanned PDFs; outside Docker it is a system program.

### New in this release (since 0.3.29)

- `/compact` shows "Compacting the chat…" in the web chat (a pulsing line, replaced by the result) and keeps Telegram's "typing…" on until it ends. Before, nothing showed until the end.
- Web: a message sent to an empty chat that was deleted meanwhile (by another tab, the phone or a chat list refresh) is answered in a new chat and the page follows it, instead of failing with "unknown chat". Telegram already did this since 0.3.18. Editing an old message still needs its chat.
- `atomic_write_text` no longer retries a refused file replacement on Windows. If Windows holds the file at that instant, the save fails with an error instead of being retried.
- Tests: the Node tests run inside the Python suite (`tests/web/js/test_node_suite.py`), the model picker tests are repaired, and the Windows-only tests are gone. The suite has 2343 tests and skips none on Linux.
- Docs: one backlog (`docs/project/backlog.md`) and status texts brought up to date.

### Agents that work together (0.2.0 to 0.3.6)

- **Subagents**: `task_start` hands work to a fresh worker or to one of your own agents; results return to the parent chat; `task_list`, `task_result`, `task_wait`, `task_steer`, `task_send` and `task_stop` manage them. Workers inherit the parent's tools by default; limits are 5 running tasks per chat and 10 per user.
- **Background processes**: `process_start`, `process_list`, `process_read`, `process_wait` and `process_stop` run long commands without the terminal timeout.
- **Task tray** above the message box lists the running tasks of the chat, with Stop, Steer and Open chat.
- **Default worker model** per agent, and a per-agent limit of tool rounds.

### Permissions and agent administration (0.3.0 to 0.3.1, 0.3.17)

- Six **permission levels** (minimal to admin) with a ceiling per user (`max_level`) and a level per agent; one module decides which tools exist at each level and refuses the rest.
- **Approval rules** (allow, ask, deny) in three layers: installation, user, agent. Cards offer once, this session or always.
- Tool groups `permissions` and `agents` let an operator-level agent manage other agents, always through an approval card and audited. An agent can only lower its own level.
- Tool group `secrets` (admin only): list, copy, rename and delete secrets by name, never seeing a value.
- **Approval explanations**: a small model adds one or two sentences to each approval card, on the web and on Telegram (0.3.8).

### Memory (0.3.3 to 0.3.7, 0.3.20, 0.3.25 to 0.3.29)

- **Context compaction**: older turns are summarised when the context passes a threshold, configurable per agent; `/compact [share] [focus]` does it by hand (0.3.7).
- **`session_search`**: agents search their own conversations, by meaning or by exact text.
- **Associative memory** is quieter: minimum similarity 0.45, one hint per turn, a dialog that shows the query and each fragment, and the built-in RECALL manual indexed as a read-only source.
- **Dream mode**: a `dream_tasks.md` queue, `for_user.md`, compaction of its own context, optional read-only workers, rereading reviewed chats, a trace viewer and no stated time limit.
- A "Memory habits" block in the prompt of every agent with memory, and a fuller default dream prompt (0.3.20).

### Files, voice and Telegram (0.3.7 to 0.3.22)

- **PDFs**: `make_pdf` builds them; `view_image` lets a model look at an image or at PDF pages; `read` and `folder_read` return a PDF as page text; `move` copies bytes so PDFs and images arrive intact.
- **`send_file`** (also from a terminal machine) and **`notify_telegram`** tools, with a short retry for connections that cannot be opened.
- **Voice messages** in the web UI (microphone button, transcription shown under the player) and a per-user language (`models.stt_language`).
- Each turn records whether it came from Telegram or the web UI, and the model sees it.
- Telegram: approval cards are deleted once answered and leave a tick or a cross; actions stay inside the collapsible block.

### Web interface (0.2.1 to 0.3.29)

- **New look** (0.2.3): editorial layout, accent colours, decorative architecture, self-hosted fonts, site icons and a pixel-art logo (0.2.2, 0.3.2).
- **Settings > Providers** (0.3.17): sign in or renew the Claude subscription from the web, and each user can set their own OpenRouter key, checked before it is saved.
- **Claude plan usage** and `/usage` (0.3.8), moved under the message box as two dots with a floating panel (0.3.23).
- **Stop button**, with a note that says whether you stopped the reply or a new message did (0.3.29); a reply that is cut by a restart is saved and shown as cut off (0.3.11).
- **Activity dot** in the chat list for working, answer ready or waiting for approval; the unread state is kept on the server (0.3.12).
- **Faster chats**: the chat list no longer re-parses every transcript and long chats open with the last 25 messages and a "load earlier" bar (0.3.15 to 0.3.16).
- Attachments: paste with Ctrl+V, drag files onto the chat, thumbnails, players for audio and video, and a viewer for pictures and PDFs (0.3.11, 0.3.25, 0.3.29).
- **Agent editor** with a section list on the left; **animated profile pictures** shown at the start of replies (0.3.19 to 0.3.21).
- Phones: the top bar stays visible with the keyboard open, the side drawer works, Enter writes a new line on touch screens and the selectors become icons (0.2.1, 0.3.3, 0.3.14).

### Fixes worth knowing

- Reloading the page while an agent replies no longer draws the reply twice (0.3.15).
- Editing earlier messages after pressing Stop works (0.3.14); tool calls and results come back when a chat reloads.
- Background processes that no longer exist after a restart are marked `lost` (0.3.13).
- Reading a task with `task_result` no longer wakes the chat (0.3.5).
- The Telegram bot answers in a new chat when its empty chat was deleted before the first message (0.3.18).

### Known gaps

The open list is in [backlog.md](backlog.md). The main ones: no zero-downtime restarts, no cost control, Node tests are not run by the GitHub workflow, and `/dreamstart` and `/dreamcancel` are not in the web chat.
