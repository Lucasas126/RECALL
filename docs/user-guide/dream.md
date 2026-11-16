# dream mode

background sessions in which an agent reviews its recent conversations with the
user and curates its own files: personality (`SOUL.md`), memories, skills, mind
palace and journal. it is the deeper, batch version of a "self-improvement
review": nothing is answered to the user during a dream, the agent only learns.

Status: **implemented** (files, prompt assembly, `self` and folder tool groups,
review queue, runner, schedule, commands, Telegram notification, the web API and
the [web UI](#web-ui) pages). this page replaces the earlier
separate "sleep mode" and "dream mode" ideas in the README; they are one feature.

## goals

- the agent keeps improving between chats without blocking them.
- what it learns lives in plain files the user can read and edit.
- the per-agent file layout mirrors other agent frameworks' (`SOUL.md`, `memories/`,
  `skills/`) plus RECALL's mind palace, journal and review queue.
- everything a dream did is inspectable in the web UI: reasoning, tool calls,
  results.

## per-agent files

```
data/<user_id>/<agent_id>/
  SOUL.md                  personality / identity (the only source; missing = empty)
  soul_history/            last 5 versions of SOUL.md, written before every change
  memories/
    MEMORY.md              agent's own notes; char-limited; injected in every chat
    USER.md                what it knows about the user; char-limited; injected
  skills/<name>/SKILL.md   procedures; frontmatter name + description
  mind_palace/             free-form notes (existing source)
    journal/YYYY-MM.md     what was done / learned, appended by dreams
    for_user.md            questions, ideas and findings to bring up with the user
    dream_tasks.md         work the user asked for the next dream
  conversations/           chats (existing source)
  dream/
    review_state.json      review queue (see below)
    sessions/<ts>.jsonl    full trace of each dream session
    sessions/<ts>.json     session summary (status, timings, tokens, changes)
    .lock                  held while a dream runs
    .cancel                cancel request for the running session (see cancel)
```

`SOUL.md` is the agent's only personality source: there is no personality field
in the agent YAML, and a missing `SOUL.md` is an empty personality (the file is
created on the first write). `SOUL.md` and `memories/*` are written by the agent
(tools) and by the user through the [web API](#web-api).

in code: `AgentFilesSource` (`agent:` source, `src/core/sources/agent_files.py`)
holds `SOUL.md`, `soul_history/`, `memories/` and `skills/` and refuses any other
path of the agent folder; `DreamStateSource` (`dream`, not readable by tools)
holds the review state, traces and lock. memory entries are separated by a line
holding only `§`. `for_user.md` and `dream_tasks.md` entries are top-level `- `
list items; items checked as `- [x]` count as done.

## prompt assembly (chats and dreams)

system prompt = `SOUL.md` + `MEMORY.md` + `USER.md` + skills index (name and
description of each skill; the body is loaded on demand with `skill_view`) +
up to 3 open items from `for_user.md` + current extra blocks (associative
notice, dream tasks notice, memory habits notice, MCP notes). the memory habits notice (`_MEMORY_HABITS_NOTICE` in `src/agent/agent.py`) goes to every non-disposable chat of an agent that has memory and can write it: what belongs in `MEMORY.md` / `USER.md` and what in the palace, never deleting to make room, the palace structure being the agent's own to evolve. dreams add the agent's dream prompt after the profile blocks.
the assembly lives in `src/agent/profile.py`; empty blocks are left out.

files are read when a chat's agent is built: changes made by a dream take
effect in **new chats**, never mid-conversation (by design).

## review queue

every chat message is reviewable. the queue is not a list of chats but a
watermark per chat:

```json
{"chats": {"20261031-125849-fbdc1b": {"reviewed_until": 14, "reviewed_at": "..."}}}
```

- a chat is **pending** when its number of turns is greater than
  `reviewed_until` (new chats start at 0). a chat that continues after being
  reviewed becomes pending again, with only the new part to read.
- the dream lists pending chats (`dream_pending`) **most recent first** (by the time of the chat's last turn; each
  entry also has `first_new_at` and `last_turn_at`), so what it reads first is the latest word on a subject: a bug
  or a plan an older chat leaves open may be settled in a newer one, and the dream prompt tells the model to check
  that before it saves something as open. reads them
  (`dream_read_chat`, returns only the unreviewed turns plus a little context;
  calling it again for the same chat continues with the next part, so a long
  chat is read to its end before it is marked) and calls
  `dream_mark_reviewed(chat_id)` when done. marking stores the turn count
  captured by the session's last read of the chat, so messages that arrive
  during the dream stay pending.
- a session that fails, hits its budget, times out or is cancelled leaves the
  rest pending; most recent first next time.
- deleted chats are dropped from the state.
- [disposable chats](memory.md#disposable-chats) are never pending and cannot be read
  by the dream; once converted to normal chats they start at turn 0 and are reviewed.

## a dream session

1. **trigger**: schedule, `/dream start` (Telegram or web chat), or the web UI.
   no-op if nothing is pending, unless started manually.
2. **lock**: take `dream/.lock` (file lock, works across the web and Telegram
   processes). if it is held, report "a dream is already running".
3. **run** in a background thread (`DreamManager` in `src/service/dream.py`),
   outside the chat worker queue, so chats with the agent keep working. one
   dream per agent at a time.
4. **loop**: a tool-calling loop like the chat one (`DreamRunner` in
   `src/agent/dream.py`, non-streaming), with the dream model, the dream toolset
   and limits (`max_tokens` input+output; provider usage,
   or chars/4 when the provider reports none; `max_minutes` of wall-clock time,
   see [time limit and cancel](#time-limit-and-cancel)). every event (reasoning,
   assistant text, tool call, tool result, warnings, errors) is appended to
   `sessions/<ts>.jsonl` as it happens.
5. **finish**: write `sessions/<ts>.json` (status `completed | budget_exhausted
   | timed_out | failed | cancelled`, steps, tool calls, errors, tokens, chats
   reviewed, pending before/after, files changed with unified diffs for `agent:`
   files, the final reply as `summary`, and `timing`: `max_minutes`,
   `deadline_at`, `warning_at`, `stop_requested_at`, `closed_at`), release the
   lock, optionally notify by Telegram. files changed are computed by comparing
   the agent files and palace before and after, so edits a chat makes during the
   session are included.

session ids look like `20261103-040000-ab12`. trace events are JSON lines with
`ts`, `type` (`session_started`, `usage`, `reasoning`, `assistant`, `tool_call`,
`tool_result`, `wrap_up_warning`, `force_closed`, `error`, `session_finished`),
`step` and type-specific fields (`name`, `arguments`, `result`, `error`, `text`,
...). `session_started` carries the limits and `deadline_at`; `force_closed`
carries `reason` (`timed_out | cancelled`), `message` (for example "deadline
reached, model call aborted"), `aborted_model_call`, `skipped_tool_calls` and,
after an abort, `model_thread_ended`.

### how a session ends

a session ends only when the agent calls `dream_finish(summary)` (status
`completed`, the summary is the one shown in the session list), or when a limit
or a cancel stops it. a reply without tool calls does **not** end it: the
runner keeps the reply, adds one user message ("the dream session is still
open: N chats are still pending... call `dream_finish` when done", traced as
`continue_nudge`) and calls the model again. `dream_finish` ends the session
after the other tool calls of the same reply run (traced as
`finish_requested`); an empty summary is refused. after 3 replies in a row
without tool calls the session is closed anyway (status `completed`, traced as
`idle_closed`, the last reply is the summary), so a model that never calls the
tool cannot spin to its budget.

### time limit and cancel

every session has a wall-clock limit, `dream.max_minutes` (default 5, 1–120).

- **wrap-up warning**: before each model call the runner checks the time left.
  once it is 1 minute or less (half the limit for `max_minutes: 1`, so 30 s), it
  appends one user message, once per session: about 1 minute left, finish now:
  append the journal entry, call `dream_mark_reviewed` only for the chats fully
  reviewed, then call `dream_finish` with a short summary; otherwise the session
  is closed.
  it is traced as `wrap_up_warning` and its time is `timing.warning_at`. a
  separate, also one-time, token warning comes at 80% of `max_tokens` (none when
  `max_tokens` is 0, no cap).
- **context compaction**: a long session can outgrow the model's context window
  (many chats read in one context). when the next call's context passes about 55%
  of the window (200k tokens when the provider does not know it), RECALL appends a
  one-time message: the older part will be compacted after the next step, so save
  now what you learned and mark the chats you finished. on the step after that (or
  at once past 80%), the dream model writes notes on everything between the opening
  message and the latest step; they replace that part, appended to the opening with
  the exact review progress (chats marked, chats read up to which turn, chats still
  pending), which RECALL keeps outside the context. if the notes fail, the part is
  dropped and the progress note alone stays. traced as `compaction_warning` and
  `compacted` (tokens before and after); the summary call counts toward the budget.
- **deadline**: the session is force-closed with status `timed_out`. the close
  never interrupts a tool call: a tool that is running when the deadline passes
  finishes, its result is traced, the remaining tool calls of that reply are
  skipped and no other model call is made. a model call in flight is aborted:
  each call runs in a worker thread and the runner waits for it in short slices
  (`poll_seconds`, 0.5 s), checking the deadline and cancel requests between
  them. on a stop it cancels the call's cancellation token, which makes the
  shared HTTP transport (OpenRouter and Ollama) cancel the request at once, stops waiting, discards
  any partial or late reply (its tool calls are never applied) and traces
  `force_closed` with "deadline reached, model call aborted".
- **abandoned calls**: the runner waits up to 2 s for the aborted call's
  thread to end and then goes on. the thread is a daemon: with the built-in
  providers it ends right away; a provider that ignores the token keeps it until its own
  HTTP timeout or reply (the reply is dropped), so at most one such thread per
  aborted session lingers, and it never blocks process exit. a warning is
  logged and `model_thread_ended: false` is traced in that case.
- **cancel**: `POST /api/agents/<id>/dream/cancel`, the **Cancel dream**
  button or `/dream cancel` use the same safe stop, with status `cancelled`. a
  session run by this process is stopped directly. a session run by the other
  process (web vs Telegram; the run lock is cross-process) is cancelled through
  `dream/.cancel`, a file holding the session id: the runner checks it before
  each model call, between tool calls and while it waits on a model call, so the
  stop lands within about half a second. the runner removes the file when the
  session ends, and a new session clears a stale one.
- chats that were not marked reviewed stay pending, as with any other end.

### dream toolset

read (scoped to the agent, existing guards apply):
- `dream_pending`, `dream_read_chat` (at most 40 new turns per call, plus 2
  turns of context; long chats are read in parts, each call continuing the
  last; with `from_turn` it rereads any chat, also one already reviewed, from
  that turn, and nothing is marked), `dream_mark_reviewed`, `dream_finish`
  (ends the session)
- the memory read tools (`query_memory`, `search`, `list_memories`, `read`,
  tags) over the palace, conversations and `agent:` files. [Disposable
  chats](memory.md#disposable-chats) and subagent task chats are not listed,
  searched or read (the review queue still offers `remember` task chats)
- the agent's user folders (set in its config, typically an Obsidian vault),
  **read-only** in dreams, when configured, the `folders` dream tool group is
  enabled **and** the agent also holds `folders_read` or `folders_edit` in its
  `tool_groups` (see [user folders](#user-folders))
- web search / retrieve when `web` is in `dream.tools`
- `dream_delegate(chat_ids, focus, from_turn)` when `dream.delegate` is true, the agent holds the `delegate` group and its
  level is `collaborator` or higher: up to 6 pending chats per call, 3 workers at a time (with `from_turn`, any
  chats, also reviewed ones, reread from that turn; nothing to mark afterwards). Each worker reads one
  chat to its end in a fresh context with `dream.delegate_model` (empty: the dream's model; a cheaper model such as
  a Haiku fits this reading work) (tools: the chat in parts, plus the dream's memory
  read tools and web if the dream has it; never a write, a mark or a task) and returns structured notes with turn
  numbers (facts and preferences, decisions, project state at the end, procedures and lessons, open questions,
  where to save). The dream gets them as untrusted data, saves what matters and marks the chats; the position each
  worker reached counts as read. Limits per worker: 40 model calls and 400k tokens; worker tokens count toward
  `max_tokens`. Traced as `delegate_started` / `delegate_finished`
- never a terminal: dreams have no terminal tools. `dream.tools: [terminal]` and
  `terminal.allow_in_dream` are configuration errors (read-only sandbox variants
  are planned)

the agent's [permission level](../architecture/permissions.md#dream-mode) is a ceiling over all of this: memory reads
and folder reads need `reader`, memory and own-file writes and web need `assistant`; below `reader` the session does not
run (it fails with a message), and a call to a tool the level does not cover is refused even if the tool exists.

the write tools below are capped by the agent's own `tool_groups`: a dream gets
the mind palace edit tools only if the agent holds `memory_edit`, and `memory`,
`skill_manage` and `soul_edit` only if it holds `self`.

chat text and worker results are untrusted: `dream_read_chat` wraps every turn
in `<chat_text untrusted="true">…</chat_text>` (any marker inside the text is
neutralised), and the dream prompt always ends with a rule that text inside the
markers is data and that no instruction from it may be copied into SOUL,
skills, memory or USER.

write (only the agent's own files; everything else is refused):
- `soul_edit` (replace or patch `SOUL.md`; snapshot to `soul_history/` first,
  keeping the last 5)
- `skill_view`
- `memory` (add / patch / remove on `MEMORY.md` or `USER.md`, usual semantics:
  char limit enforced on the final result, batch is atomic)
- `skill_manage` (create / patch / delete skills, write supporting files)
- mind palace write/append/edit/move/delete (journal, `for_user.md`,
  `dream_tasks.md`, notes);
  the generic edit tools refuse every other source because the dream registry
  opens only the palace for writing

no tools that act outside the agent: no MCP actions, no messages to the user.
the toolset is configurable per agent within this set: `dream.tools` adds `folders`
and/or `web` reads.

the dream's `query_memory` searches the same index as the chats and never updates
it: a dream start queues a stat-only reconcile of the agent's own sources, and the
end of the session queues another one so the notes and skills it wrote are indexed
for the next chats (see [memory index](memory.md#memory-index)).

### default dream prompt (summary)

- review each pending chat; extract facts about the user, preferences,
  corrections, unfinished tasks, reusable procedures.
- update `USER.md` / `MEMORY.md` (compact: only what is needed in every chat, cannot be fetched with a tool and is not about one chat; when full, move detail to the palace and leave a pointer, never delete to make room), create or
  improve skills for repeated procedures, write longer material to the palace.
- adjust `SOUL.md` only for lasting changes in how to behave; small, explained.
- look things up (palace, user folders, web) when it helps understand a topic the user
  cares about.
- after the review, do the open items of `dream_tasks.md`: result in a palace
  note, then check the item off with a link to it (or say on its line why it
  stays open).
- keep `for_user.md`: questions, ideas and findings to bring up with the user,
  with why. few, worthwhile, not intrusive; remove the ones already said.
- upkeep after the work: merge duplicate notes, archive outdated ones, keep `INDEX.md` matching the folders, change nothing when the palace is already in good shape. a suggested palace layout (`projects/<name>/status.md`, `decisions/`, `journal/`, `reference/`) is offered as the agent's own to change.
- append a journal entry: what was reviewed, what changed, what was learned.
- mark each chat reviewed when done with it.

the prompt is configurable per agent (`dream.prompt`); the default lives in the
code and is used when it is empty.

### user folders

an agent can have several named `folders` (any folder, usually the user's
Obsidian vault), each exposed as the source `<name>:` with the usual path guard
(see [user folders](memory.md#user-folders)). in chats, the `folders_read` tool
group gives `folder_list`/`folder_read`/`folder_search` over every folder and
`folders_edit` adds `folder_write`/`folder_append`/`folder_edit`/`folder_move`/
`folder_delete`, except on folders marked `read_only`. in dreams the folders are
always read-only, and readable only with the `folders` dream tool group. the
folders must exist (saving a missing one is a `400`); hidden entries
(`.obsidian/`, `.git/`) are skipped by listings, searches and the index. folders
with `indexed: true` feed associative memory. the safe/auto [overlay](../architecture/obsidian-overlay.md)
is still planned.

### for the user

open entries of `for_user.md` (questions, ideas, findings) are injected in the
chat prompt (max 3) with the rule: raise at most one, only when it fits
naturally, never interrupt a task. the chat agent removes an entry once it said
it or the user answered.

### dream tasks

`dream_tasks.md` is the queue of work the user asks for a dream: research, a
point self-improvement. in a chat the agent adds `- [ ] task (date, why)` when
the user asks (chats with the `memory_edit` group get a short notice saying so);
the user can also edit the file. a dream lists the open tasks (up to 20) in its
opening message and does them after the chat review: result in a palace note,
then `- [x] task -> mind_palace:<note>`. the first `dream_finish` while a task
shown at the start is still open is refused once with the list, so the model
checks it off or notes why it stays open; a second call ends the session. a
scheduled run also starts when no chat is pending but a task is open.

## chats can write too

memory, skills and `SOUL.md` are not dream-only: the `self` tool group gives
chats the same `memory`, `skill_view`, `skill_manage` and `soul_edit` tools
(by design), so the agent can save something the moment it learns it.
dreams do the deeper, batch review. `skill_view` is always available when the
agent has skills.

## commands

same behavior in Telegram and in the web chat input (the web chat endpoint
answers them itself, as an SSE `text` event followed by `done`; they are not
sent to the model nor stored in the chat):

- in Telegram the same commands are the one-word `/dream`, `/dreamstart` and
  `/dreamcancel` (`/dream start|cancel` as text also work there); the web chat
  keeps `/dream start|cancel`, and hints name the command of the channel you use.
  the Telegram `/dream` reply is formatted: bold title (`💤 Dream · <agent>`),
  labelled lines (last session with outcome emoji and date, schedule and next
  run, pending chats, a running line while a session runs) and the last
  session's summary as a block quote that keeps its paragraphs and lists (cut at
  a paragraph or sentence near 1500 characters with "… (full summary in the web
  UI)"; omitted when empty).
- `/dream`: last session (when, status, short summary), whether a schedule is
  set and when the next run is, and a hint: "use `/dream start` to start a new
  session".
- `/dream start`: starts a session for the current agent and answers at once
  ("dream started"; or "already running"; or "nothing pending, starting
  anyway"). it does not block the chat. manual starts work even when
  `dream.enabled` is false.
- `/dream cancel`: asks the running session to stop safely (see
  [time limit and cancel](#time-limit-and-cancel)) and answers at once; "no
  dream session is running" otherwise. while a session runs, `/dream` hints at
  `/dream cancel` instead of `/dream start`.
- other `/dream ...` arguments get a usage hint.

## web UI

a saved agent's page in the **Agents** tab has three tabs besides the agent
form (`src/web/static`):
- **Soul & memory**: `SOUL.md`, `MEMORY.md` and `USER.md` editors with their
  character count against the limit, and the SOUL history (view a version,
  restore it after a confirmation). the agent form's personality field is only
  shown for a new agent: after the agent is created, the page writes that text
  with `PUT /api/agents/<id>/soul`. the agents API itself has no personality
  field, so saving the agent form never touches `SOUL.md`.
- **Dream** (every number has a stepper, unit, allowed range, *Default: X* hint and a reset button, see [defaults and reset](agents.md#defaults-and-reset)): schedule (manual only, every N hours or daily at HH:MM, plus the
  enabled switch), model (free text or the model picker, whose provider tabs
  list OpenRouter, Ollama and Claude subscription models; empty = the
  agent's default chat model, else the user's; a `claude_subscription:<model>` reference uses the user's own
  Claude login, see [claude subscription](claude-subscription.md)), reasoning effort, `max_tokens`, `max_minutes`, dream read
  tools, memory limits, the dream prompt (the default prompt is
  the placeholder; it can be copied for editing or reset to empty) and the
  Telegram notification. API validation errors are shown under the form.
- **Dream sessions**: a status box (running or not, "cancelling…" after a
  cancel, last session with its summary, schedule and next run, pending chats)
  with **Start dream** and, while a session runs, **Cancel dream** (after a
  confirmation), polled every few seconds while a session runs; the sessions list (date, trigger,
  status, chats reviewed, duration, tokens) and a detail view that replays the
  trace in order: step markers, token usage, reasoning, assistant text, tool
  calls with pretty-printed arguments, collapsible tool results (long ones cut
  with a "show all" button), the wrap-up warning, the forced close and errors,
  plus the summary, the timing (limit and deadline, warning, stop request,
  close) and the files changed with colored unified diffs for `agent:` files.

pages are deep-linkable with the URL hash: `#agents/<agent_id>`, `/soul`,
`/dream`, `/dreams` and `/dreams/<session_id>`; a link opened before logging
in is followed after login. dates use local `YYYY-MM-DD HH:MM`, the format of
the `/dream` reply and the Telegram notification.

`/dream` replies in the web chat are shown as a dashed "RECALL" notice instead
of an agent message; like on the server, they are not kept in the chat.

## configuration

per agent, in `.config/<user_id>/agents/<agent_id>.yaml`:

```yaml
dream:
  enabled: false
  every_hours: null          # run every N hours, or
  daily_at: "04:00"          # run once a day at this local time (HH:MM)
                             # both null = manual only; both set = invalid
  model: null                # null = the agent's default chat model, else the user's
  reasoning_effort: medium
  max_tokens: 500000         # total input+output for the session; 0 = no cap
  max_minutes: 5             # wall-clock limit; wrap-up warning 1 minute before, then force-closed
  tools: [folders]           # optional read groups: folders (needs a folder read group), web (opt-in)
  prompt: ""                 # empty = built-in default
  notify_telegram: false     # "dream session completed" + where to see it in the web
  delegate: false            # read-only chat-reading workers (needs the delegate group, level collaborator+)
  delegate_model: null       # model of those workers; null = the dream's model
folders: []                  # user folders, edited in the agent's Folders tab (see [files and media](files-and-media.md) and [memory](memory.md))
```

The maximum sizes of `SOUL.md`, `MEMORY.md` and `USER.md` are application-wide (`memory_limits` in
[settings.yaml](../reference/settings-yaml.md#memory-limits)); an agent file has no such key.

the schedule runs in whichever process is up (web or Telegram), checked every
minute; the lock and the `last_started_at` in `review_state.json` stop double
runs. `enabled` switches the schedule only. the next run counts from the last
session start; before the first one it counts from `scheduled_since`, the moment
a process first saw the schedule, so enabling a schedule never starts a session
at once. scheduled runs are skipped while nothing is pending.

`tools` defaults to `[folders]`; web is opt-in and needs Tavily; folders needs at least one
entry in `folders` and a `folders_read`/`folders_edit` tool group. `terminal` is rejected.
`reasoning_effort: null` sends no reasoning request. model calls (steps) are counted and shown, never
capped. `max_tokens` 0 (no cap) or 1000–100000000, `max_minutes` 1–120; limits are 100–50000 chars.

## web API

JSON routes under the logged-in user's session, like the other `/api/agents`
routes (`404` for agents the user does not own):

| method | route | result |
| --- | --- | --- |
| `GET` | `/api/agents/<id>/dream/settings` | `{dream, default_prompt}` |
| `PUT` | `/api/agents/<id>/dream/settings` | saves `{dream}`; `422` for both schedules or an unknown dream tool group |
| `GET` | `/api/agents/<id>/dream/status` | `{running, cancelling, current_session_id, last_session, schedule: {enabled, every_hours, daily_at, next_run}, last_started_at, last_finished_at, pending_chats}` |
| `POST` | `/api/agents/<id>/dream/start` | `202` with `{status: started \| started_nothing_pending \| already_running, session_id, pending}` |
| `POST` | `/api/agents/<id>/dream/cancel` | `202` with `{status: cancelling \| not_running, session_id}`; returns at once, the session ends after its running tool call (see [time limit and cancel](#time-limit-and-cancel)) |
| `GET` | `/api/agents/<id>/dream/sessions` | summaries, newest first, without `diffs`; a session without summary is `running` or `interrupted` |
| `GET` | `/api/agents/<id>/dream/sessions/<session_id>` | `{id, summary, events}`: the summary (with diffs) and the full trace |
| `GET` / `PUT` | `/api/agents/<id>/soul` | `{content, chars, max_chars}`; `PUT {content}` snapshots the old version |
| `GET` | `/api/agents/<id>/soul/history` | `[{version, saved_at, chars}]`, newest first (at most 5) |
| `GET` | `/api/agents/<id>/soul/history/<version>` | `{version, content}` |
| `POST` | `/api/agents/<id>/soul/history/<version>/restore` | makes that version current (the current one is saved first) |
| `GET` / `PUT` | `/api/agents/<id>/memories/<memory\|user>` | `{target, content, chars, limit, entries}`; `PUT {content}` over the limit is `400` |

## telegram notification

when `notify_telegram` is on and the user has a linked Telegram chat, a finished
session sends one message: "dream session completed" (or failed / out of
budget / reached its time limit / cancelled) for the agent, with where to find
it in the web UI (Agents -> agent name -> Dream sessions -> session date, the
date as listed there). when the web UI's public address is configured
(`web.public_url` or `RECALL_PUBLIC_URL`, see [config](../reference/settings-yaml.md#web-authentication)),
the message adds `Open it: <public_url>/#agents/<agent_id>/dreams/<session_id>`;
it is sent with Telegram formatting (bold title with the outcome emoji and agent
name, an excerpt of the session summary of up to 600 characters as a block
quote, omitted when empty; plain text if formatting is rejected) and the URL is
a real link (LAN addresses too). the link opens the session directly, after the login form when needed. the message goes to every Telegram id mapped
to the user in `TELEGRAM_ALLOWED_USERS` (private chat ids equal user ids), via
the Bot API with `TELEGRAM_BOT_TOKEN`; without a token it is skipped, and send
failures are logged without affecting the session.
