# design: asynchronous subagents and messages between agents

Status: **design only, nothing here is implemented.** Every "today" statement was checked against the code on branch `docs/subagents-design`; every name marked *(new)* is a proposal, not an existing API. Backlog items served: "Subagents" and "Messages between agents" in [the backlog](backlog.md#from-the-replica-report-2026-11-05) (items 4 and 5), and the foundation of items 2 (scheduled jobs / proactive Telegram) and the "agent-to-agent delegation" line of the orchestrator follow-ups.

## 1. goals and non-goals

Goals

- An agent can hand a task to another worker and keep going. The worker is either a fresh ephemeral agent or an **existing agent of the same user** (own SOUL, memory, tools). Subagents and agent-to-agent messages are one mechanism: a *task*.
- Asynchronous like a typical `delegate_task`: several tasks in parallel; the parent may keep working, wait, or end its turn. When a task finishes, its result **returns to the parent chat as a new message that wakes the parent agent** with no user input, and the agent then tells the user.
- The parent can list live tasks, steer one (send a correction) and stop one.
- A child inherits only what the parent grants and never more than the parent has. Manual approvals are shown to the **user in the parent chat**, attributed to the task.
- Hard limits from day one (depth, parallelism, wall clock, tokens). A provider 429 (the Claude subscription has a 5-hour plan limit) stops the task and reports; it never retries in a loop.
- Works across the web and Telegram processes, and survives restarts.
- Reuses what exists: `ChatService.send`, per-chat workers, `CancellationToken`, disposable chats, chat sidecars, `ApprovalBroker`, `RunContext`, `exclusive_file_lock` / `NonBlockingFileLock`.

Non-goals

- No cross-user delegation (User A and User B never reach each other's agents).
- No durable generation queue and no automatic resume of a crashed child (the overview already states that scheduling is in-process; this design adds a durable *task record*, not a durable model run).
- Not building scheduled jobs, proactive Telegram messages, background processes or clarify cards (see [section 13](#13-what-this-gives-to-scheduled-jobs)).
- No new provider calls from the framework itself: all work is a normal agent reply.
- No MCP tools for children in the first phases.

## 2. concepts and vocabulary

| term | meaning |
|---|---|
| task | one delegated unit of work. A durable record plus a running child chat. Id: 12 hex chars (same style as approval ids). |
| parent chat | the (user, agent, chat) whose agent called `task_start`. The task result returns here. |
| child chat | an internal chat holding the child's conversation: a normal `<chat_id>.jsonl` in the target agent's `conversations` folder, marked by a task sidecar *(new)*, hidden from lists. |
| target | `ephemeral` (a neutral worker built from the parent's config and provider) or `agent` (an existing agent of the same user, with its own SOUL, memory and tool groups). |
| grant | the tool groups and terminal backends (with approval modes) the child receives; always a subset of the parent's. |
| wake message | a system-originated turn written into the parent chat when a task ends, which starts a parent generation. Stored as a `user`-role turn flagged `source: "task"` *(new)* because the model API only continues from a user turn. |
| executor | the process whose worker thread runs the child. |
| chat owner process | the process that serves the parent chat's channel: `origin` `web` → web process, `telegram` → Telegram process (see [section 9](#9-telegram-and-web-process-split)). |

## 3. tools exposed to the model

One new tool group, `delegate` *(new)*, added to `TOOL_GROUP_CATALOG` and `TOOL_GROUP_TOOLS` in `src/agent/tool_groups.py` and to `_ORDER` in `src/agent/tools.py`. It is not in `DEFAULT_TOOL_GROUPS`: delegation is always an explicit opt-in, like folders and the terminal. It is also added to `DISPOSABLE_BLOCKED_TOOLS`, so a disposable chat (which cannot change state) and every child chat cannot launch tasks, which gives depth 1 by construction.

Tools are plain `@tool` functions in a new `src/agent/toolsets/delegate.py` (`src/agent/tooling.py` derives the spec from the signature; the docstring is the description, `Annotated[..., "text"]` the parameter descriptions). They return compact JSON strings. The parent identity (user, agent, chat) and the task manager come from `current_run()` / `RunContext` (`src/terminal/context.py`), the same contextvar the terminal tools use; `RunContext` gets a `tasks` field *(new)* next to `broker`. No change to `Agent` construction is needed for the parent side.

Descriptions are deliberately terse, because tool specs are sent on every model call. Measure them with the existing usage breakdown (`usage.turn_breakdown(..., self._tool_specs)`) before shipping; if six specs are too heavy, expose `task_start` plus `task_list` first (`delegation.tools` in the agent config chooses the subset).

| tool | parameters | returns | description sent to the model |
|---|---|---|---|
| `task_start` | `goal: str`; `agent: str = ""` (empty = ephemeral, else an allowed agent id); `context: str = ""` (facts the child needs; it sees nothing else of this chat); `tools: list[str] = []` (groups to grant); `backends: list[str] = []`; `model: str = ""`; `timeout_s: int = 0` (0 = default); `wait: bool = False` | `{"id","status"}`; with `wait`, also `"result"` and the final status | "Run a subtask in a separate hidden chat. Returns an id at once; the result arrives later as a message. `wait` blocks until it ends. Give a self-contained goal." |
| `task_list` | `status: str = ""` | one line per task: `id status age title` | "List your tasks." |
| `task_result` | `id: str`; `full: bool = False` | result text (capped, truncation marked) or the current status | "Get a task's result or status." |
| `task_steer` | `id: str`; `text: str` | `{"id","status"}` | "Send a correction to a running task." |
| `task_send` | `id: str`; `text: str` | `{"id","status"}` | "Reply to a task that has finished (or is idle) so it keeps working with its full context; no need to launch a new task." |
| `task_stop` | `id: str` | `{"id","status"}` | "Stop a task; its partial result is kept." |
| `task_wait` | `ids: list[str]`; `timeout_s: int = 60` | results of the tasks that ended, ids still running | "Block until the tasks end or the timeout (max 120 s)." |

Rules enforced in the handler, never trusted to the model:

- `agent` must be in the parent's `delegation.targets` and in `user.agents` of the *same user*; there is no `user` parameter, so a cross-user target cannot even be expressed.
- `tools` ⊆ parent's effective groups ∩ `delegation.allowed_groups` (and ∩ the target agent's own groups for an existing agent). `delegate` is never grantable.
- `backends` ⊆ the parent's `terminal.backends`; see [section 6](#6-permissions-and-approvals).
- `task_wait` is a blocking tool like `ApprovalBroker.request`: it polls and honours the parent's `CancellationToken`, so a steering message from the user ends the wait (`GenerationCancelled`). Its cap (120 s) keeps a worker thread from being held for long. A result read through `task_wait`, `task_result` or `wait=true` is marked consumed, so no wake message is sent for it.
- `task_send` (design request, 2026-11-05): the child chat is a normal persisted chat, so continuing a task is just another `ChatService.send` into it. Allowed when the task is `done`, `timed_out` or `failed` (not `owner_lost`) and its chat still exists; the task returns to `running`, counts against the parallel limits again, gets a fresh timeout and token budget (the previous usage stays in the record), and ends with a new wake as usual. Capped at `max_followups` (5) per task. other agent frameworks does not have this: its `delegate_task` children are one-shot (`steer` only reaches a child that is still running), so a follow-up there means a new task with the context rewritten. Here it costs almost nothing extra, which is why it is in phase 2. `task_steer` stays for tasks that are still running.
- `instructions: str = ""` on `task_start` (ephemeral target only): the system prompt of the worker, written by the parent. For an existing-agent target it is ignored and the agent keeps its own SOUL; `goal` and `context` are the task message.
- Errors are returned as `error: ...` strings (the convention of `Agent._run_tools`), never raised.

## 4. lifecycle and state machine

```
queued -> running <-> awaiting_approval -> done
                  \-> failed | cancelled | timed_out
```

| state | meaning | entered by |
|---|---|---|
| `queued` | accepted but a parallel limit is reached | `task_start` over the per-parent/per-user limit (bounded queue, otherwise the tool returns an error) |
| `running` | the child chat has an active reply | the executor starts the run |
| `awaiting_approval` | derived, not stored: the broker holds a pending approval of this task | `ApprovalBroker.request` |
| `done` | the child's final answer is the result | the child's reply ended normally |
| `failed` | provider/tool error, limit hit, or `owner_lost` after a crash; `reason` says which (`rate_limited` for a 429) | exception from the run, or recovery |
| `cancelled` | `task_stop`, parent chat deleted/discarded, service shutdown | `cancel` |
| `timed_out` | wall clock or token budget exceeded | watchdog |

A separate `wake` sub-state tracks delivery to the parent: `none` → `pending` → `claimed` → `delivered`, or `consumed` (the parent read the result itself), or `orphaned` (parent chat gone).

Where state is stored: a per-user task store *(new, `src/service/tasks.py`)* at `data/<user>/.tasks/` (outside every agent folder; the leading dot cannot collide with an agent id, whose pattern `^[A-Za-z0-9_-]{1,64}$` forbids it, so nothing that lists agent folders sees it). One JSON file per task, written with `atomic_write_text`, read-modify-write under `exclusive_file_lock(.tasks/.state.lock)` (the pattern of `DreamStateSource.update_state` and `UserPreferencesStore.update`).

Record (no secrets, same sensitivity as a chat): `id`, `user_id`, `parent {agent_id, chat_id, generation_id, task_id|null}`, `depth`, `chain [agent ids]`, `target {kind, agent_id}`, `child {agent_id, chat_id}`, `title` (≤80 chars), `goal`, `grant {groups, backends:{name:mode}}`, `limits`, `status`, `reason`, `attended`, timestamps, `usage {in,out}`, `result {text, truncated}`, `error {code,message}`, `owner {process_id, pid, channel}`, `wake {state, ...}`, `commands []` (inbox, see [section 9](#9-telegram-and-web-process-split)).

The child chat's grant and parentage are also written as a sidecar `<chat_id>.task.json` *(new)* beside `.origin.json` and `.disposable.json` (`ConversationsSource`, `delete_chat` removes it with the others). The `Agent` reads its restrictions from the stored sidecar the way it reads `is_disposable(chat_id)` today, so the restrictions survive an agent rebuild or restart and cannot be dropped by the model.

## 5. execution model

**Who runs the child.** The launching process: the one whose worker thread executes the parent's `task_start`. It already holds the parent's live run (feed, cancellation token, approval broker). It creates the child chat and drives it with the existing public path, `ChatService.send(user, target_agent, child_chat, text)`, in a consumer thread owned by the task manager (one daemon thread per running task, bounded by the parallel limit). Because the child is an ordinary chat run, it gets the per-(user, agent) slot, its own worker, cancellation, `_RunFeed`, steering and `interrupted` semantics for free; different chats of the same agent already run in parallel. A side effect worth keeping: `update_agent` / `delete_agent` refuse while the slot has active or queued work, so an agent cannot be edited under a running child.

**Child construction.** The service builds the child agent through a task-aware factory *(new)* instead of the plain `agent_factory(user_id, agent_id, chat_id)`:

- ephemeral: parent's provider config, a neutral profile (`AgentProfile()` is already the default in `Agent.__init__`) plus a short task brief, `associative_memory: off` (saves a model call per turn), memory on within the chat so a steer keeps context. The model comes from `model` or `subagents.default_model`, set through the existing per-chat model override (`ConversationsSource.set_model`, validated like `set_chat_model`, ZDR rules included).
- existing agent: that agent's SOUL, memory (read), model and tool groups intersected with the grant. The first user turn carries a provenance header (`From agent <name>, task <id>:`) then goal and context.

**Hidden chat.** Child chats reuse the disposable machinery: no Dream review, no index chunks, removal after the user's retention period. Two gaps must be closed (they are real behaviours of today's code):

1. `list_chats`, `list_all_chats`, `latest_chat` return every chat; a task sidecar must exclude the chat unless `include_internal=True`. `prune_empty` (called by `create_chat` and `list_chats`) deletes unused empty chats not in `_kept_chats`; a child chat must be exempt, or be created with its first turn already written.
2. `Agent._disposable` currently also removes the terminal tools (`DISPOSABLE_BLOCKED_TOOLS` contains them). A child with an explicit terminal grant (phase 3) needs "excluded from learning" and "tools blocked" to be two predicates: learning exclusion stays shared with disposable; the blocked set for a task chat is computed from the sidecar grant. Until phase 3 children are plain disposable and have no terminal.

**Concurrency.** Limits in [section 7](#7-limits-and-cost-control). Queued tasks start in FIFO order when a slot frees (the manager checks on every terminal transition).

**Completion.** The consumer thread ends when the child's feed ends. The manager reads the last assistant turn from the child chat file, caps it, writes the record (`status=done`, `result`, `usage`), then schedules the wake.

**Wake: re-entering the parent chat.** A new service method `ChatService.send_system(user, agent, chat, text, *, source, meta, steer=False)` *(new)* builds a `_MessageRun` and goes through `_enqueue` and the normal worker (`_work` → `_run` → `Agent.invoke`). The wake text is the *user-role* content, framed as untrusted data (see [section 10](#10-security-and-failure-modes)):

```
[RECALL task result, not written by the user]
task 3f9a1c0b22d4 "compare X and Y" (agent: research) - done, 41 s, 12k tokens
<task_result untrusted="true">
...capped result...
</task_result>
Tell the user what matters. Ask for the full text with task_result if you need it.
```

`Agent.invoke` gets an optional `meta` that `_log_turn(..., **extra)` persists on the user turn (it already accepts `**extra`; `source` and `task_ids` are added to the whitelist in `ChatService.get_chat` so the UI can render the turn as a result bubble instead of a user bubble). The generation emits ordinary `ChatEvent`s on a fresh `_RunFeed`, so everything that already follows a reply (web `GET .../stream` via `follow`, the Telegram event loop) works unchanged.

| parent state when the child ends | behaviour |
|---|---|
| idle (turn ended) | wake run starts at once (debounced ~1.5 s so several results finish into one message) |
| mid-generation (user or earlier wake) | `_enqueue` with `steer=False` appends behind the active run: **no cancellation of the user's reply**. Results that arrive meanwhile are merged into one pending wake run (one parent generation, not N). |
| parent blocked in `task_wait`/`wait=true` | the waiting tool returns the result inline; wake state becomes `consumed` |
| user sends a message while a wake is queued | existing steering applies: queued same-chat runs are cancelled and recorded as an empty interrupted turn *after* the wake turn was logged by `Agent.invoke`, so the model still sees the result in history on the user's turn |
| parent chat deleted | `delete_chat`/`discard_chat` first cancel that chat's non-terminal tasks (extending `_discard_replies` usage, which cancels without deleting); a result that finds no chat is `orphaned` and shown in the task panel and, if linked, as one plain Telegram notice (`TelegramNotifier`, usable from either process) |
| parent agent deleted/edited | refused while its slot is active (existing `AgentBusy`), so children keep it busy |
| service shutdown | `ChatService.close()` cancels running tasks (`cancelled`, reason `service shutdown`); pending approvals are already denied by `ApprovalBroker.close()` |

**Guarantees when a result arrives while the user is talking (design requirement: it must behave smoothly and never jam).**

1. A wake never cancels or truncates a reply in progress, and it never blocks the user: the user's message is always accepted at once.
2. Several results that arrive while the parent is busy become one pending wake, delivered as one turn after the current reply ends. The turn lists each result with its task id and title.
3. The wake turn is framed as untrusted data from a task, not from the user, and tells the model to answer the user's latest request first if it is still unanswered, then report the result briefly, without repeating what it already said.
4. If the user writes while a wake turn is queued or generating, normal steering applies (the unfinished generation is cancelled and kept as an interrupted turn), and the user's turn sees the result in history because the wake turn is logged before generation.
5. If a task ends while the parent is in `task_wait`, the result goes through the tool and no wake is sent.
6. Wake-started turns in a row without a user message are capped (`max_auto_wakes`, 3), so tasks cannot keep the parent talking to itself.
7. Required tests (phase 2): result arrives while idle, mid-reply, mid-wake, with a user message at the same moment, two tasks ending together, task ending during `task_wait`, parent chat deleted at the same time.

**Crash and restart recovery.** Each running task's executor holds a `NonBlockingFileLock` on `.tasks/<id>.run.lock` for the task's life (same primitive and Windows/POSIX support as the dream `run_lock`; the OS drops it when the process dies). Recovery, run at service start and by the poller: a non-terminal task whose lock can be acquired is dead → `failed`, reason `owner_lost` (the child chat keeps its partial work; nothing is resumed or re-executed, avoiding duplicated side effects). A terminal task with `wake.state` `pending` or a stale `claimed` is delivered by the chat owner process. Delivery is idempotent: before writing the wake turn the claimant checks the parent chat for a turn whose `task_ids` already contains the id.

## 6. permissions and approvals

Capability rules (all computed once at launch, stored in the record and the chat sidecar):

1. **Never more than the parent.** `grant.groups ⊆ parent_groups`; `delegate` never; MCP never in v1; memory/self write groups are not grantable (children are disposable-like). Folder groups only if the parent has them *and* the child's agent config has the folder.
2. **Terminal.** Only if the parent has `terminal`, the call names backends, each backend is in the parent's `terminal.backends`, and the target has them (existing agent) or inherits them (ephemeral). Per backend the child's approval mode is the **stricter** of parent and target (`manual` stricter than `smart` stricter than `off`). `off` is never inherited by a child; for the `local` backend the cap is `manual` in v1. The `local` protected paths need no extra work: they are enforced inside `TerminalSession.decide` before every mode and before the unattended handling (`assess` → `protected`, "no one can approve access"), using `settings.yaml terminal.protected_paths` and the built-in set, so a child on `local` cannot reach SSH keys, secrets files, `.env`, credentials or process environments in any mode, and `terminal.allow_local` still gates the backend. The hardline blocklist applies the same way.
3. **Per-agent switch.** `delegation` block on the agent *(new, `AgentConfig.delegation`, `extra="forbid"` like `terminal`)*: `enabled` (bool), `targets` (allowlist of the user's agent ids; empty = ephemeral only), `allowed_groups`, `max_parallel`, `timeout_seconds`, `max_tokens` (can only lower the application limits), `tools` (which task tools to expose). A model validator requires the block when the `delegate` group is selected (as `terminal` is required by its group) and rejects unknown targets. `src/config/field_defaults.py` and `docs/reference/agent-yaml.md` follow.
4. **Same user.** Targets are resolved only through `ChatService._owned_agent` (membership in `user.agents`) and the task store lives under the user's data folder; nothing in the tool or the HTTP API accepts another user id.

Approval flow:

- The child runs with its own `RunContext` whose `chat_id` is the child chat but which carries `approval_target = (parent agent, parent chat)`, `task {id, title, agent_name}` and `interactive` *(new fields)*. `TerminalSession.decide` already asks `run.broker.request(user, agent, run.chat_id, run, ...)`; the request is registered under the **parent** coordinates and its payload gains `task`, so `ApprovalBroker.pending(user, parent_agent, parent_chat)` (what `GET .../approvals` and `list_approvals` return) lists it, `resolve_approval` validates against the parent's (agent, chat), and the owner check (`item.user_id != user_id` → `ApprovalForbidden`) is unchanged.
- The child's tool thread blocks in `request()` (poll every 0.2 s) until answered, timed out (`terminal.approval_timeout_seconds`, default 300 s → denied), cancelled (task stopped → `Outcome(False, "cancelled")`, and `TerminalSession.decide` raises `GenerationCancelled`) or shutdown. The task shows `awaiting_approval` meanwhile. The wall-clock timer of the task is *paused* while it waits (otherwise a slow human times out the child).
- `interactive` = the launching run's `RunContext.interactive` AND the task was not launched unattended. A task with `interactive=False` goes through the existing unattended branch of `TerminalSession.decide`: `manual` → denied, dangerous → denied, `smart` non-dangerous → auto. No new policy code. That is the rule for runs without a watching user.
- A terminal approval card raised in a turn that was started by a wake message carries a one-line note "this turn was triggered by a task result" (see injection, [section 10](#10-security-and-failure-modes)).

## 7. limits and cost control

Application defaults in `settings.yaml` under a new `subagents:` block *(new, loaded in `src/config/settings.py` like `terminal`)*; an agent's `delegation` can only lower them.

| limit | default | scope | enforced by |
|---|---|---|---|
| `max_depth` | 1 (a child cannot launch; may be raised to 2) | chain | sidecar `depth`; `delegate` blocked in child chats |
| `max_parallel_per_parent` | 3 | parent chat | manager, counted from the store |
| `max_parallel_per_user` | 4 | user (User A and User B separately) | manager; plus `queue_max` 5, then the tool errors |
| `max_launches_per_turn` | 5 | one parent turn | handler counter |
| `max_auto_wakes` | 3 consecutive wake-started turns without a user message | parent chat | `meta` chain counter; after that the result is stored and the user is told, not the agent woken |
| `timeout_seconds` | 600 (max 3600) | task | watchdog thread cancels the child's token → `timed_out`; excludes time awaiting approval |
| `max_tokens` | 200 000 in+out | task | watchdog sums `usage {in,out}` that `_remember_reply` persists on each assistant turn; per-call granularity needs a small `Agent` usage callback (`_track_usage` already computes it) |
| per-turn steps | `agent.max_tool_hops` (settings, default 5) | child turn | existing `AGENT_MAX_TOOL_HOPS` |
| `result_max_chars` | 8000 | wake and `task_result` | manager truncates, marks it, full text stays in the child chat |
| cooldown on 429 | until the provider says, min 5 min | user + provider | see below |

Rate limit: a provider limit surfaces as an exception from the run (`ClaudeSubscriptionError("Claude plan limit or rate limit reached: ...")` in `src/providers/claude_subscription/provider.py`, `OpenRouterRateLimited` with code `rate_limited` in `src/providers/openrouter.py`). The consumer thread records `failed`, reason `rate_limited`, writes a per-user `launch_blocked_until` in the store, and the wake message says so. While blocked, `task_start` returns an error instead of launching; **no task-level retry exists**. A queued task is failed the same way. This stops fan-out against a shared 5-hour window: with the Claude subscription every child draws on the same plan as the parent and the user's own chats, so default parallelism is intentionally low.

Spend: the existing usage plumbing records tokens per child turn; a per-user/day cap belongs to the "Cost control" backlog item and plugs into the same watchdog (a pre-launch check and a per-task ceiling).

## 8. ui

Web (`src/web/static`, vanilla JS):

- **Task card in the timeline.** `task_start` is already a stored tool step (timeline segment `{type: "tool", index}` into `actions`, with the call arguments and the result JSON carrying the task id). The renderer special-cases that tool name and draws a card: title, target agent, status pill, elapsed time, token use, a collapsible result, buttons Open chat / Steer / Stop. Live status comes from polling `GET /api/agents/{a}/chats/{c}/tasks` *(new)*, started and stopped like `approvalTimerTick` (1 s timer, a request every 3 ticks while any task of the open chat is active). No change to the stored timeline format in phase 2; a dedicated `task` segment type is optional later.
- **Wake bubble.** `get_chat` returns `source`/`task_ids` for wake turns; the renderer shows them as a collapsible "Task result" bubble, not as a user message. The parent's reply follows as an ordinary assistant message.
- **Wake while the chat is open.** The polling above sees `chat.active`; the existing `openChat` logic attaches through `GET .../stream` (`follow`) which replays the reply so far and follows it live. If the chat is not open, the chat list already marks generating chats.
- **Task panel.** A list of the user's tasks (`GET /api/tasks`), grouped running / finished, with Open chat. "Open chat" opens the child chat by id; the normal chat list never shows it. Internal chats need a read-only banner (like the disposable banner) and a Steer box that calls `task_steer` semantics.
- **Approval cards** reuse `syncApprovals`/`showApproval`: cards come from `GET .../approvals` of the **parent** chat, with a "task" line from the payload. `ChatEvent` gets kind `task` *(new)* for live status; `_event_stream` in `src/web/app.py` lists the kinds whose `data` is a dict and must include it.
- A later `GET /api/tasks/events` SSE replaces polling (phase 6).

Telegram (`src/telegram/bot.py`):

- Unknown event kinds fall into the `else: continue` of `_send_generation`, so adding `task` to `ChatEvent` is safe for the bot before it handles it.
- Short status messages only: `🧩 Task started: <title> (<agent>)` with a `[Stop]` button, edited in place on completion (`✅ done`, `⌛ timed out`, `⏹ cancelled`, `⚠ plan limit reached`). Callback prefix `tk` (existing: `p`, `r`, `ap`), data `tk:<id>:s`, owner check identical to `approval_callback` (`card.user_id != session.user_id`).
- Approval cards reuse `_send_approval`/`approval_callback`; `_approval_text` adds `Task: <title> (<agent>)`. They currently exist only while a reply stream is being consumed; the bot-level consumer in [section 9](#9-telegram-and-web-process-split) sends them when the parent is idle too.
- A wake reply is streamed with `_ReplyStream` into the private chat. `_send_generation` is coupled to an `Update`; it needs a variant taking `(bot, telegram_chat_id, user, agent, chat)` (the private chat id equals the Telegram user id, which `TelegramNotifier` already relies on). `/tasks` lists live tasks.

## 9. telegram and web process split

Today (verified): `web.py` and `run_telegram.py` start two processes, each with its own `ChatService`, `_AgentSlot`s, agent cache, `ApprovalBroker` and `_RunFeed`s, all in memory. They share only files: chat JSONL and sidecars, the preferences store (`exclusive_file_lock`), the dream `run_lock` (`NonBlockingFileLock`) and cancel file, and config fingerprints. Both start the dream scheduler; the lock makes it safe. Approvals are strictly per process: a card is delivered through the run's own event stream, `resolve_approval` only sees its own broker, and Telegram cards exist only while `_send_generation` is consuming events. A cached `Agent`'s context is built from the file once and is *not* refreshed when another process appends to the same chat; that is why Telegram resumes only chats with `origin="telegram"` (`latest_chat(origin=...)`) and ignores web-only chats.

Resolution, in four rules:

1. **One chat, one driver process.** The chat's `origin` marker names the channel that drives it; each `ChatService` declares the origins it serves *(new constructor argument, set by `create_app` → `{"web"}` and `build_application` → `{"telegram"}`; tests and the CLI serve none)*. Wakes, steering and approvals of a chat are executed by the process serving its origin. The executor of a task is always the process running the parent's turn, which for an interactive chat is the owner by construction, so the normal case never crosses a process.
2. **The task store is the shared truth.** Both processes read it; only the executor mutates a running task (the other process may only append to `commands`).
3. **Commands inbox.** Steer, stop and approval decisions from the *non-executor* process are appended to the record's `commands` under the store lock. The executor polls its own running tasks (1 s, the same cadence as the broker's 0.2 s loop but cheaper, and exactly how `DreamRunner` polls the `.cancel` file) and applies them: `steer` → `ChatService.send` on the child chat, `stop` → cancel, `approve|deny` → `ApprovalBroker.resolve`. `ChatService.resolve_approval` gains a fallback: if the id is not in the local broker but the store lists it as pending for an executor in another live process, it writes the command instead of raising `ApprovalNotFound`. Pending approvals are mirrored into the record (`approvals: [{id, command, risk, expires_at, ...}]`, no secrets beyond what the card shows) so the other process can draw the card.
4. **Poller.** A new `ChatService.start_task_poller()` *(new)* runs next to `start_dream_scheduler` (the same hook in `create_app` and Telegram `post_init`; a daemon thread, every 2 s, plus an in-process fast path). Per user it lists `.tasks/` by mtime and: (a) applies commands to tasks it executes; (b) claims `wake.state=pending` of tasks whose parent chat's origin it serves (claim under the store lock, then `send_system`); (c) runs crash recovery; (d) pushes changes to in-memory per-process notification queues consumed by the channel.

Scenarios:

| scenario | executor | who wakes the parent | how it is shown |
|---|---|---|---|
| web chat launches a task | web | web, in-process | SSE/`follow` + task card polling; approvals via SSE and `syncApprovals`; Telegram not involved unless `notify` |
| Telegram chat launches a task | Telegram | Telegram, in-process | `_ReplyStream` for the wake reply; cards and status as bot messages |
| Telegram chat opened in the web | Telegram | Telegram | web shows the saved chat and task cards from the store (read-only live view is not possible, same as a running Telegram reply today); Stop and Approve work through the commands inbox |
| executor crashed or restarted | none | process serving the parent's origin after it starts; if none for `wake_grace` (10 min) and Telegram is linked, one plain notice via `TelegramNotifier` | `failed/owner_lost` card, partial child chat openable |
| scheduled job (future) | whichever process wins the job lock | process serving the parent chat's origin | same as above, the cross-process wake path is what the poller already does |

The web learns of a wake started in its own process through the polling described in section 8; it learns of one started elsewhere through the saved chat on the next refresh, consistent with what exists today for any running reply of another process. No new dependency, no message broker and no change to the "web and Telegram share files only" rule is needed. What is not solved and is called out in section 12: answering approvals across processes depends on the owner being alive (otherwise the task is already `owner_lost`).

## 10. security and failure modes

| risk | mitigation |
|---|---|
| **Prompt injection through child results** (a child reads the web or a file; its output reaches a parent that may hold more privileges, e.g. terminal) | the wake and `task_result` text is wrapped as untrusted data with an instruction to treat it as data; length-capped; wake turns are flagged `source: "task"` and rendered distinctly; a terminal approval raised in a wake-started turn says so; dangerous or manual commands still need a human; child grants never exceed the parent's, so a child cannot launder access. Residual risk remains and should be stated in the user docs. |
| Runaway loops (task → wake → task ...) | depth 1, per-turn launch cap, parallel caps, `max_auto_wakes`, timeouts, token ceiling, rate-limit cooldown |
| Cross-user | store under the user's data folder, every API call takes `user_id` from the session or Telegram mapping, agent targets resolved only via `user.agents`, Telegram callbacks check `card.user_id == session.user_id`, approval ids remain owner-checked |
| Secrets | goal/context are model-written: not logged beyond 200 chars (as the terminal does); the child gets no secrets file access (`local` protected paths, no MCP, no env); the task record never stores tool arguments or credentials; audit rows (`write_audit`, per agent) gain `task_id` |
| Cross-process deletion | `delete_chat` only checks its own process's slot; it must also refuse while the store lists a non-terminal task for that chat, and the hourly `cleanup_disposable_chats` (run by both processes) must skip task chats of live tasks (retention is ≥ 1 day, tasks ≤ 1 h, so this is a safety net) |
| Stale agent context | the child is driven only by the executor; the target agent's cached `Agent` for that child chat is built from the file at first use |
| Telegram wake for a chat that is not the session's current chat | the reply is sent with the agent name; the user's next message still goes to the session chat. Accepted for v1; revisit with chat hand-over |
| Duplicate wake after a crash | idempotent check on the parent chat before writing |
| Orphans | child chat of a vanished task is removed by retention; a task without chat is shown as `orphaned` |
| Provider limit | stop and report (section 7); the parent's own turn may fail the same way and says so |

## 11. phased build plan

Each phase ships and is tested alone. New test files follow the existing style (`SteeringFixture` in `tests/test_service_steering.py`, `tests/test_disposable_chats.py`, `tests/test_terminal_approvals.py`, `tests/test_terminal_telegram.py`).

| # | phase | scope | files touched (expected) | tests |
|---|---|---|---|---|
| 1 | **Synchronous-looking launch** | `delegate` group with only `task_start(wait=true)`; one ephemeral child in a hidden disposable-like chat; result returned as the tool result; task record in the store (no commands, no wake); limits (depth 1, 1-2 parallel, timeout, token watchdog, 429 → failed, no retry); no terminal, no write tools; `subagents:` settings and `delegation` agent block | `src/agent/tool_groups.py`, `src/agent/tools.py`, `src/agent/toolsets/delegate.py` (new), `src/terminal/context.py`, `src/service/tasks.py` (new), `src/service/chat.py` (task manager wiring, hidden-chat filters in `list_chats`/`latest_chat`/`_kept_chats`, cancel without delete), `src/core/sources/conversations.py` (task sidecar), `src/config/models.py`, `src/config/settings.py`, `settings.yaml`, `src/config/field_defaults.py`, docs | store round-trip and locking; hidden from lists and not pruned; tool returns result with a stub agent; timeout; token cap; rate-limit mapping; group not grantable beyond parent; disposable parent cannot launch |
| 2 | **Async wake-up** | `task_start` without `wait`; `task_list/result/steer/stop/wait`; `send_system` + `_enqueue(steer=False)`; wake turn storage and `get_chat` fields; coalescing; delete/discard cascade; restart recovery (lock probe) | `src/service/chat.py`, `src/agent/agent.py` (`meta`), `src/agent/toolsets/delegate.py`, `src/service/tasks.py`, `src/web/app.py` (task routes, `task` event kind), `src/web/static/app.js`/`style.css` (card, wake bubble, polling), `src/telegram/bot.py` (status messages, `_send_generation` variant, `/tasks`), `src/utils/i18n.py` | wake while idle / mid-generation / user steering a queued wake; merging; chat deleted; owner crash → `owner_lost`; `node --check` and a `.cjs` test for the card state |
| 3 | **Approvals and terminal grants** | `RunContext` approval target, `task` and `interactive`; broker keyed by parent; attribution in cards; terminal grant with stricter-mode rule; split "excluded from learning" from "tools blocked"; unattended path; timer pause while awaiting | `src/terminal/approvals.py`, `src/terminal/context.py`, `src/terminal/session.py` (no policy change, plumbing only), `src/agent/tools.py`, `src/agent/agent.py`, `src/agent/tool_groups.py`, `src/web/static/app.js`, `src/telegram/bot.py` | broker tests with a child run; card shows in parent chat; deny on timeout; protected path denied on `local` in all modes; `interactive=False` denies manual; mode never exceeds parent |
| 4 | **Cross-process** | origins served, commands inbox (stop/steer/send; no cross-channel approval relay, design decision),  poller, wake claim, notification queues; Telegram bot consumer | `src/service/tasks.py`, `src/service/chat.py`, `src/web/app.py`, `web.py`/`run_telegram.py` wiring, `src/telegram/bot.py` | two `ChatService` instances on one temporary data dir (as `test_config_pickup` does): stop/steer/approve from the non-executor, claim races, lock-based recovery |
| 5 | **Agent-to-agent** | existing-agent targets (with per-target `remember: true`, see section 12), `delegation.targets` allowlist, chain/cycle refusal, provenance header, target's tool intersection, agent editor UI | `src/service/chat.py`, `src/service/tasks.py`, `src/config/models.py`, `src/web/static/app.js`, docs/reference | allowlist, cross-user target rejected, A→B→A refused, B's grants ∩ parent's |
| 6 | **Polish and budgets** | task panel, `GET /api/tasks/events` SSE, per-user daily cap hook, Telegram `Stop` buttons everywhere | web and bot files | SSE replay, cap |
| 7 (optional) | **Unattended entry point** | `launch(..., unattended=True, trigger=...)` without a parent run; see section 13 | `src/service/tasks.py` | unattended policy table |

## 12. decisions of the owner (2026-11-05)

| # | question | decision |
|---|---|---|
| 1 | Does a finished task always wake the parent? | Yes by default; `wake=False` for fire-and-forget. It must behave smoothly: the user keeps talking while tasks run and results interleave safely ([guarantees in section 5](#5-execution-model)). |
| 2 | Can an existing-agent child write memory? | Ephemeral children: no. Existing agents the user already has deployed (for example Orion launched by the assistant): yes, they should write to their own memory in that session. Implemented in phase 5 as a per-target `remember: true` in `delegation.targets`: the child chat is then a hidden but ordinary chat of that agent (not disposable), keeps the agent's own `memory_edit` tools, and may be reviewed by Dream. If it proves complicated (concurrent memory writes, Dream review of hidden chats), it moves to the backlog and v1 targets stay read-only. |
| 3 | Default numbers | Accepted: depth 1, **3 parallel tasks per parent**, 4 per user, 600 s (10 min), 200k tokens, 8k result chars. Agents must know the limits (see below). |
| 4 | Ephemeral persona | The parent writes the worker's prompt (`instructions` on `task_start`); there is no inherited SOUL. An existing agent keeps its own SOUL and settings. |
| 5 | Terminal for children | Yes, `manual` only (as recommended). |
| 6 | Answer approvals from the other channel | No. An approval is shown in the channel of the parent chat only. The commands inbox of phase 4 stays for stop/steer/send, crash recovery and future jobs, but approvals are not relayed. |
| 7 | Continue a finished child (new, design idea) | Yes: `task_send` (section 3). It does not exist in other agent frameworks; here it is cheap because the child chat persists. |

| 8 | Steering a running task (design request) | The USER can steer a running task from RECALL too, not only the parent agent: the task card (web) and a Telegram button/command send a correction into the running child (same mechanism as `task_steer`: the child's current generation is interrupted, its partial reply kept, the correction appended, generation continues). Exposed as `POST /api/tasks/<id>/steer {text}` and Telegram `/task <id> <text>`; also `stop`. Phase 2 (same-process), cross-process via the commands inbox in phase 4. |
| 9 | Wake vs. user collisions | other agent frameworks tends to cut the agent when a subagent result arrives mid-reply; RECALL does NOT cut it: the guarantees of section 5 apply (verify in tests). |

**Agents must know the limits.** The limits are written in three places so the model does not have to find out by failing: (a) the `task_start` tool description states the numbers (3 running per parent, 10 minutes each, depth 1); (b) the `delegate` group adds a short notice to the system prompt in the same way the terminal group describes its backends; (c) the error returned when a limit is hit says which limit and what to do (`error: limit reached: 3 running tasks; wait for one to finish or use task_wait`). A knowledge-pack page `references/delegation.md` (tools, limits, wake behaviour, when not to delegate) and the user docs are deliverables of phase 1/2 (AGENTS.md rule).

## 12b. superseded open questions

The original six questions and recommendations are kept below for the record.

1. **Does a finished task always wake the parent?** A wake costs one full parent generation (and plan quota). Recommended: yes by default, `task_start(wake=False)` records the result silently for fire-and-forget tasks.
2. **Should a child on an existing agent be able to remember (write memory, be reviewed by Dream)?** Recommended: no in v1 (disposable semantics, safest); a later per-target `remember: true` opt-in.
3. **Default numbers** (depth 1, 3 per parent, 4 per user, 600 s, 200k tokens, 8k result chars): recommended as written; the Claude plan window argues for the low parallelism.
4. **Ephemeral persona:** neutral worker plus task brief, or a fork of the parent's SOUL? Recommended: neutral, with the parent's provider and a cheaper default model, so the child cannot act "as" the parent.
5. **Terminal for children:** allow in phase 3 at all? Recommended: yes, `manual` only, `off` never inherited, `local` capped at `manual`; revisit after use.
6. **Answering approvals from the other channel** (Telegram card for a web chat's task): worth the commands inbox? Recommended: yes in phase 4, because the same inbox is required for scheduled jobs and crash-safe stop/steer.

## 13. what this gives to scheduled jobs

Backlog item 2 needs "an agent run without a user message and an unattended approval policy". This design builds those two pieces and a third:

| job need | provided here |
|---|---|
| a run with no user message | `send_system` (phase 2) and the task consumer: a task with `trigger: "schedule"` and no parent run is the same child execution; the model call is an ordinary `ChatService.send` on an internal chat |
| unattended approval policy | `RunContext.interactive=False` plus the existing `TerminalSession.decide` branch: manual and dangerous denied, smart non-dangerous auto, protected and hardline always refused. No new policy |
| proactive message to the user | a wake (or a job result) delivered as a generation on a chat whose origin is `telegram`, through the Telegram consumer that does not need an `Update` (phase 2/4), with `TelegramNotifier` for a plain fallback |
| survive restarts, no double run | the task store plus `NonBlockingFileLock` per job id (the pattern of the dream `run_lock`), so both processes can run a scheduler like `DreamManager.start_scheduler` and only one executes |
| budgets | the same limits (timeout, tokens, 429 cooldown, per-user parallel cap) |
| user visibility | task cards, `/tasks`, task panel and orphan handling |

Still missing and out of scope: the schedule store and its parsing (cron-like or reminder text), a tool or UI to create jobs, per-job notification targets and quiet hours, and the backlog's Reminder step. When scheduled jobs are built they add a `jobs.json` and a scheduler loop; execution, limits, approvals policy, delivery and recovery are the ones above.
