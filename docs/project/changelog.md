# changelog

The notes of each release are on the GitHub Releases page. This file groups what changed by topic, starting with 0.4.0, which
sums up everything since 0.2.0 (5 November 2026): 0.2.x and 0.3.x, 34 releases between 5 and 15 November.

## 0.4.0

### Upgrading

- Every user needs `max_level` in `user.yaml` (0.3.0); `terminal.allow_in_dream` no longer exists.
- Remove `prompt_cache.trim_slack_percent` and `conversation_messages_retained` from a custom `settings.yaml`: compaction replaces them (0.3.7).
- `dream.max_steps` is gone; an old key in an agent file is ignored (0.3.25).
- `questions.md` in an agent's palace is now `for_user.md` (0.3.8).
- The daily task token budget is removed and tasks have no token cap unless `max_tokens` is set (0.3.6).
- Sent audio and video are stored with the message and take disk space (0.3.11).
- The Docker image installs Tesseract (English and Spanish) for scanned PDFs; outside Docker it is a system program.

### New in this release (since 0.3.29)

- `/compact` shows "Compacting the chat…" in the web chat (a pulsing line, replaced by the result) and keeps Telegram's "typing…" on until it ends. Before, nothing showed until the end.
- Web: a message sent to an empty chat that was deleted meanwhile (by another tab, the phone or a chat list refresh) is answered in a new chat and the page follows it, instead of failing with "unknown chat". Telegram already did this since 0.3.18. Editing an old message still needs its chat.
- `atomic_write_text` no longer retries a refused file replacement on Windows. If Windows holds the file at that instant, the save fails with an error instead of being retried.
- Tests: the Node tests run inside the Python suite (`tests/test_node_suite.py`), the model picker tests are repaired, and the Windows-only tests are gone. The suite has 2343 tests and skips none on Linux.
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
