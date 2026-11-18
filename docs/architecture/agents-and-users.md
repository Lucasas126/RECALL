# agents and users

The platform is multi-user. Each **user** (a human, identified by `user_id`) owns one or more **agents**; each agent belongs to
exactly one user. Users never see each other's agents, data or memories, and nothing is shared implicitly between agents.
How to manage them day to day is in the [agents guide](../user-guide/agents.md); the files are described in the
[user.yaml](../reference/user-yaml.md) and [agent.yaml](../reference/agent-yaml.md) references.

```
user --owns--> agent --scoped--> sources (mind_palace, conversations, agent files, dream state, user folders)
```

## identity resolution

Channels map an external identity to a `user_id`; unknown identities are rejected. There is no registration flow.

- **web**: the Argon2 `password_hash` in `.config/<user_id>/user.yaml` is checked at login and the id is stored in a signed,
  HTTP-only session cookie (see [web authentication](../reference/settings-yaml.md#web-authentication)).
- **Telegram**: `TELEGRAM_ALLOWED_USERS` maps `<telegram_id>=<user_id>` entries, several users per bot ([Telegram](../user-guide/telegram.md)).
- **CLI** (`cli.py`): the `USER_ID` / `AGENT_ID` values of `.env`.

After that the target agent is chosen (explicit selection, else the user's last used agent (`agent_id`), else the first one in `agents`; new chats nobody picked an agent for use the
user's default agent, `default_agent_id`, falling back to the first one),
its `SourceRegistry` is built scoped to user and agent, and the message is handled.

## data layout

```
data/<user_id>/<agent_id>/
  SOUL.md  soul_history/  memories/  skills/     the agent's own files (source `agent`)
  mind_palace/  conversations/                    notes and chats
  dream/                                          dream state (not readable by tools)
  .index/                                         vector index (Chroma + manifest)
```

Profile pictures live beside these folders, never inside one: `data/<user_id>/.avatars/<agent_id>.png` and `_user.png` (the user's own),
so no agent tool can list or write them (see [profile pictures](../reference/api.md#profile-pictures)).

Everything an agent produces lives under its user's folder. The vector index belongs to the agent, so retrieval never
crosses users or agents. See [dream mode](../user-guide/dream.md#per-agent-files) for the file roles.

## what an agent instance is

- **identity**: id, name, description and its own files: `SOUL.md`, memories, skills.
- **data scope**: its mind palace, conversations and user folders.
- **tool policy**: the tool groups and MCP servers it may use.
- **chat behavior**: streaming, reasoning, memory, associative-memory mode, optional default model, dream settings.

Model ids and providers belong to the **user**, not the agent: the user's defaults apply unless the agent sets
`chat_config.model` or a chat has its own selection (precedence: chat > agent > user; see
[models and providers](../user-guide/models-and-providers.md)). Embeddings use the user configuration.

## isolation

Isolation is enforced in one place: the per-agent `SourceRegistry` (path resolution and traversal guard) and the per-agent
index namespace. A user folder belongs to the agent that lists it in `folders:`; paths are validated when the agent is saved.
Binding an Obsidian vault through the safe/auto overlay (a per-agent shadow tree) is a planned design, see
[overlay](obsidian-overlay.md); today a user folder pointing at the vault is the way to use one.

## tool separation

- each agent has a **tool-group policy**: a selection from the shared catalog (`memory_read`, `memory_edit`, `web`, `self`,
  `folders_read`, `folders_edit`, `terminal`, `delegate`, see [tools](../reference/tools.md#tool-group-catalog));
- each agent also chooses which shared **MCP servers** it uses, optionally read-only, with a tool selection and extra
  instructions; they act on the agent's own user's connected account ([MCP servers](../user-guide/mcp.md));
- legacy `allow`/`deny` lists are still accepted as an additional per-tool filter;
- dream sessions use their own toolset ([dream mode](../user-guide/dream.md#dream-toolset)); recall mode (planned) would restrict it further.

The effective toolset is `agent_policy ∩ mode_toolset`. For a tool call: (1) is the tool in one of the agent's selected
groups, (2) does the current mode expose it, (3) does the target `source:path` resolve inside one of this agent's sources?
If any check fails the call is rejected before touching data.

## subagent tasks

The `delegate` tool group (async `task_start`, the wake, task tools; see [delegation](../user-guide/delegation.md)) adds a second kind of chat run, owned by the
`TaskManager` in `src/service/tasks.py`, which `ChatService` creates and binds to every reply through `RunContext.tasks`:

- **Store.** `data/<user_id>/.tasks/` holds one JSON record per task (read-modify-write under `.state.lock`, atomic writes), `<id>.run.lock` held by the executing process
  (a free lock on a non-terminal task means its executor died: `failed`, `owner_lost`) and `.state.json` with the per-user rate-limit cooldown.
  The folder starts with a dot, so it cannot collide with an agent folder.
- **Worker chat.** A hidden chat of the launching agent, created with a `<chat_id>.task.json` mark (`ConversationsSource`, `ChatTask`) written before the transcript exists, plus the
  disposable flag. `chats()`, `latest()` and `prune_empty()` skip task chats; `Agent` reads the mark to become an ephemeral worker (the parent's `instructions` as its system prompt, no
  SOUL, memory, MCP or associative search, only the granted tool groups) and `DISPOSABLE_BLOCKED_TOOLS` keeps it from launching tasks (depth 1).
- **Run.** The manager drives the worker through the public `ChatService.send` in a daemon thread and a watchdog thread (wall clock; the token cap is enforced from a usage hook the agent calls after each
  model call, set up in `ChatService._run`). A stop uses `ChatService.cancel_chat_run`, which cancels without deleting. Provider rate limits end the task as `failed`/`rate_limit` and
  block new launches for a cooldown; nothing is retried.
- **Async tools (phase 2a).** `task_start` returns the id at once (`wait=true` still blocks, `wake=false` is fire-and-forget). `task_list`, `task_result`, `task_wait` (max 120 s, polls like
  `ApprovalBroker.request` and honours the parent's `CancellationToken`), `task_stop`, `task_steer` and `task_send` are methods of the manager (`list_for`, `result`, `wait`, `stop_task`,
  `steer`, `send`) that take an `owner` (agent id, chat id) the tools fill from `RunContext`, so a task is only reachable from the chat that launched it. A UI passes `owner=None` after its
  own user check. `steer` reuses `ChatService.send` on the worker chat: the unfinished reply is interrupted and kept, the correction is appended, and the driver thread follows the continuation's
  feed, so the task only ends when the corrected reply ends. `send` (task_send) is another `ChatService.send` into the same persisted worker chat (full context) with a new run lock, a fresh
  timeout and token cap (`base_in/base_out` keep the earlier usage in the record), at most `subagents.max_followups` per task.
- **Wake.** When a task with `wake.state = pending` ends, `TaskManager._schedule_wake` starts a ~1.5 s debounce timer (`subagents.wake_debounce_seconds`) per parent chat. The flush
  (`_deliver`) claims every unread result of the chat atomically under the store lock (`pending -> claimed`, so two flushes or two processes never deliver the same result), builds one
  text (`wake_frame`/`wake_block`: header `[RECALL task result, not written by the user]`, one `<task_result untrusted="true">` block per task, an instruction to answer the user's latest request first)
  and calls `ChatService.send_system(..., source="task", meta={"task_ids", "task_runs"}, steer=False, frame=wake_frame)`. That builds a `_MessageRun` and goes through `_enqueue(steer=False)`: it
  is only appended behind the chat's runs and never cancels the active reply. A wake that is still queued when a second result arrives is merged into (one generation); the turn is logged by
  `Agent.invoke(meta=...)` as a user-role turn with `source: "task"` and `task_ids` (`get_chat` returns both). A user message keeps ordinary steering priority: a queued or generating wake is
  cancelled like any run, and the user's turn finds the wake turn in the history because `invoke` logs it before generating. Rules: results read through `task_wait`, `task_result` or
  `wait=true` become `consumed` and never wake (tasks a `task_wait` is blocked on are skipped by the flush; a cancelled wait re-schedules); at most `subagents.max_auto_wakes` (3) wake turns in a
  row without a user message, the rest are `held` and delivered after the reply to the next user message (`release_held`, called by `ChatService.send`); delivery is idempotent (a run id
  `<task>:<follow-up n>` already listed in a turn of the chat is marked delivered, never rewritten); a cancelled task (stop, shutdown) sends no wake. Wake states: `none | pending | claimed |
  delivered | consumed | held | orphaned`.
- **Parent chat deleted.** `ChatService.delete_chat` (also `discard_chat`) first calls `TaskManager.cancel_for_parent`: the chat's running tasks are stopped (`cancelled`/`parent_deleted`,
  wake `orphaned`). A result whose chat is gone when the flush runs is `orphaned` too (the record and the worker chat stay readable); the chat is never recreated.
- **Recovery.** `TaskManager.recover()` (called from `ChatService.start_dream_scheduler`, i.e. at start of the web and Telegram processes) fails non-terminal tasks whose run lock is free
  (`owner_lost`, the parent is told) and schedules delivery of terminal tasks whose wake is `pending`, `held` or a stale `claimed` (only for chats whose origin the process serves, see the next item).
- **Process split (phase 4).** `web.py` and `run_telegram.py` are two processes with separate `ChatService`s that share only files. `ChatService(origins=...)` declares the chat origins a process drives
  (`create_app` → `{"web"}`, `build_application` → `{"telegram"}`; tests and the CLI declare none, then a process delivers its own tasks' wakes as before and claims nothing). The *executor* of a task is the
  process that runs the parent's turn; the *origin* of the parent chat (`<chat_id>.origin.json`) names the process that must deliver its wake. Mechanisms, all through the task store, no broker:
  (1) **commands inbox**: `TaskStore.enqueue` appends `{id, kind: stop|steer|send, text}` to `<task_id>.cmd.jsonl` under the store lock; `TaskManager.stop_task/steer/send` called for a task that is not
  live here but whose run lock another process holds return `{status: "queued", command}` instead of an error (`send` for a finished task just runs here). The executor's `poll_commands` (every 1 s, only
  while it has live tasks; also once more when a task ends) takes the file (`take_commands`, at-most-once), applies each through the ordinary `stop`/`steer`/`send` paths and records
  `{id, kind, status: applied|rejected|failed, note}` in the record's `commands` (`command_outcome`; the web view shows the last five). (2) **wake claim**: a finished task whose process does not serve the
  parent's origin leaves `wake.state=pending`; `poll_wakes` (every ~2 s, started by `ChatService.start_task_poller`, called from `start_dream_scheduler`, stopped in `close`) delivers the chats of its origins
  through the same `_deliver`, whose `_claim` is atomic under the store lock and writes `claimed_by` (process id) and `claimed_at`; a claim older than 60 s is taken over, so two pollers deliver once and a
  crash between claim and delivery loses nothing. (3) **recovery in the poller**: `_fail_dead` runs on every pass, not only at start-up. (4) **approvals are not relayed** (design decision 6): the executor
  mirrors `awaiting_approval` and `approval_channel` into the record only so that the other channel's task card says *awaiting approval (answer it in Web|Telegram)* (`view()["approval_elsewhere"]`); the
  approval card itself stays in the executor's channel. Task lists read the store, so every process shows the tasks of the parent chat wherever they run.
- **Task event hook.** `TaskManager.subscribe(callback)` (returns an unsubscribe function; `ChatService._tasks`) calls `callback(event: dict)` on the thread that raised it, errors are logged and
  ignored, listeners must not block. Events: `task_started` (`task_id`, `agent_id`, `chat_id` of the parent, `status`, `followup` for a task_send), `task_finished` (`status`, `reason`),
  `task_wake` (`chat_id`, `agent_id`, `task_ids`: a wake generation was queued; attach to it with `ChatService.follow`) and `task_orphaned` (`task_ids`). The web and Telegram layers
  subscribe to these in later tasks (task cards, a push of the wake reply); nothing in phase 2a does.
- **Terminal grants and approvals (phase 3).** `task_start(backends=[...])`: `TaskManager.launch` checks the names against `grantable_backends` (the parent's own `terminal.backends`, only with the
  `terminal` group; `delegate.granted_backends`), adds `terminal` to the task's groups and records `grant.backends`; the `ChatTask` mark also stores `backends`, `title` and `unattended`. `Agent._become_worker`
  narrows its copy of the config to those backends with every `approvals` forced to `manual` (so a child never has `off`/`smart`, `local` included) and `build_tools(worker_terminal=True)` keeps the terminal
  group in the otherwise disposable chat (`Agent._blocked_tools`: "excluded from learning" is separate from "tools blocked"; memory/self/folder-edit/`delegate` stay blocked). `TerminalSession.decide`
  is unchanged except for the routing: `ChatService._work` calls `TaskManager.bind_worker(context, mark)` for a task chat, which sets `RunContext.approval_target` (parent agent and chat),
  `task` (id, title, kind, worker: the attribution put in the approval payload), `approval_emit` (task events `task_approval` / `task_approval_resolved` for the parent chat, with `agent_id`, `chat_id`,
  `task_id`, `data`), `approval_wait` (pauses `_LiveTask.active_seconds`, which the watchdog compares with the timeout) and `interactive = not mark.unattended`. `ApprovalBroker.request` keys the
  pending approval by (user, parent agent, parent chat), so `ChatService.list_approvals`/`resolve_approval` and the owner check work unchanged; `pending_for_task` feeds `awaiting_approval` in
  `TaskManager.view` (task card and `/tasks`). The web polls the chat's approvals while a task with backends runs (`taskPoll` → `syncApprovals`); Telegram handles the two task events in
  `_handle_task_event` and registers the card under the parent chat. Approvals are shown only in the channel of the parent chat (no relay between channels). Unattended (`interactive=False`, for phase 7):
  the existing policy denies `manual` and dangerous, `smart` stays auto.
- **Not built yet** (later phases): other agents as workers, cross-process commands.
