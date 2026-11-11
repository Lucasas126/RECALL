# Delegating tasks to a worker

> **Status: phases 1 to 3 of the [subagents design](../project/design-subagents.md).** Tasks run in the background while the agent keeps talking, a
> finished task wakes the agent, and a worker can use the terminal with your approval of every command. Other agents as workers and cross-process control are planned and **not built yet**.

The `delegate` tool group gives an agent `task_start` and six tools to manage tasks (`task_list`, `task_result`, `task_steer`, `task_send`, `task_stop`,
`task_wait`). The agent writes a self-contained goal, the context the worker needs and the worker's own system prompt (`instructions`), and `task_start`
runs a **worker** (a fresh, short-lived agent) in a hidden chat. It returns an id at once and the agent keeps talking to you; the result comes back
later. It is useful for work that is big and independent: reading many pages, researching one question in depth, drafting alternatives.

## Results wake the agent

When a task ends, its result is written into the parent chat as a **result message** (stored as a user-role turn marked `source: "task"` with the task ids, framed
as data from a worker, not something you wrote) and the agent gets a turn to report it. What you can rely on:

- A result **never cuts a reply in progress** and never blocks your messages: it waits behind the reply that is running. Results that end at about the same time
  (within 1.5 s), or while the agent is busy, are delivered as **one** message and one reply.
- If you write while a result message is queued or being answered, your message wins as usual (the unfinished reply is kept as interrupted) and the agent still
  sees the result in the conversation when it answers you. The message tells the agent to answer your latest request first, then report the result briefly.
- At most 3 result-started turns in a row without a message from you (`subagents.max_auto_wakes`); further results are held and delivered after the reply to your next message.
- A result the agent read itself (`task_wait`, `task_result`, `wait=true`) sends no message. `wake=false` on `task_start` is fire-and-forget: no message at all.
- If you delete the chat, its running tasks are cancelled; a result that finds no chat is kept in the task record only.
- After a restart of the process, results that were waiting for their message are delivered at start-up, and a task whose process died is reported as `failed` (`owner_lost`).

## Managing tasks

- `task_wait(ids, timeout_s)`: block for results, **at most 120 s** (a message from you ends the wait). Use it only when the agent cannot go on without the result.
- `task_steer(id, text)`: correct a **running** task. Its current reply is interrupted and kept, the correction is appended and it continues.
- `task_send(id, text)`: talk to a task that has **finished** (`done`, `timed_out`, `failed`) and keep it working with its full context, instead of starting a new task. It counts
  against the parallel limits, gets a fresh timeout and token budget, is limited to 5 follow-ups per task (`subagents.max_followups`) and ends with a new result message.
- `task_stop(id)`, `task_list(status)`, `task_result(id, full)`.

## In the web app

- **Task card.** Each `task_start` step in a reply is a one-line card: the task title, `ephemeral` or the agent's name, and a status badge with the elapsed time (Running · 1m 05s, Done, Failed, Cancelled, Timed out). Open it to see the result. The card updates by itself (the page asks the server every ~3 seconds while a task of the open chat runs).
- **Stop, Steer, Open chat.** *Stop* ends a running task and keeps its partial result. *Steer* opens a small field: what you type is sent into the running worker as a correction (its current reply is interrupted and kept, your text is appended and it continues), the same as the agent's `task_steer`. *Open chat* shows the worker's own conversation in a dialog marked as internal and read-only; it is never in your chat list. **Watching a task live:** while the task runs, the dialog shows the saved conversation and then the worker's current reply as it is written (thinking, tool steps with their running indicator, text), like a normal chat; it follows the bottom only while you stay near it, and when the reply ends (or a steer starts a new one) it refreshes from the saved chat and attaches again if the task still runs. A running task's card also shows one muted line with what the worker is doing now (*Thinking…*, *Running terminal: ls -la*, *Writing…*). The live view works only in the process that runs the task: for a task started from Telegram while you use the web, the dialog refreshes the saved chat every few seconds instead, and the card shows no activity line.
- **Task result bubble.** When a task ends, the result that reaches the agent appears as a compact, collapsed "Task result" note listing the task titles, not as a message of yours. The agent's reply to it follows as a normal message and streams live if the chat is open, also after a reload.
- **Tasks panel.** The *Tasks* button in the sidebar footer (next to the theme and settings buttons) opens a list of **all** your tasks across chats and agents, newest first, with the filters *All*, *Running* and *Finished*. Each row shows the status badge, the title, `ephemeral` or the agent's name, the elapsed time, the tokens used, and where it came from (*from <agent> · <chat title>*: click it to open that chat). *Stop*, *Steer* and *Open chat* work as on the card. While anything runs the panel refreshes every ~3 s. At the top it shows **today's task usage** against the daily budget (below).
- Everything works in history after a reload, in both themes and on narrow screens. The same actions are available through the API ([subagent tasks](../reference/api.md#subagent-tasks)).

## Turning it on

Delegation is an explicit opt-in per agent, like folders and the terminal: add the **Delegate subtasks** group in the agent's
**Tools** tab, or `delegate` to `chat_config.tool_groups` in the agent yaml. The group gets default settings; the optional `delegation`
block of the [agent yaml](../reference/agent-yaml.md#delegation) tunes or switches it off (`enabled: false`). Nothing changes for agents
without the group.

## What a worker is

- **No inherited persona.** The agent writes the worker's system prompt through `instructions`; the worker does not get the agent's SOUL, memory,
  skills, files or MCP services, and it cannot ask you questions. It has the terminal only if the agent grants it backends (see
  [terminal and approvals](#terminal-and-approvals)).
- **Same provider and model as the chat that launched it**, unless the agent names another model, or its `delegation.default_model` (Default worker model in the Delegation block) is set (checked like a chat's own model choice,
  including the zero-data-retention rule). Reasoning effort follows the launching chat too.
- **Tools only by grant.** By default none. The agent may grant `web` (web search), and only if it has the group itself and `delegation.allowed_groups`
  allows it. Memory, folders, MCP and `delegate` can never be granted, and the terminal only through `backends`, so a worker cannot do more than its parent and cannot launch tasks (depth 1).
- **A readable brief.** In *Open chat*, the task message the worker received is shown as a **Task brief** card (who asked, task id, the *Goal* and a collapsible *Context*), and follow-ups and corrections as small labelled notes, instead of a plain message bubble. The worker still receives the same plain text.
- **A hidden chat.** The worker's conversation is a chat file of the launching agent, marked as a task chat: it is not in the chat list, is never the
  "latest chat", is not pruned while empty, is not reviewed by Dream and not indexed, and is removed after the disposable-chat retention
  period. A disposable chat cannot launch tasks.

## Handing a task to another agent

A fresh worker is not the only choice: an agent can hand a task to **another agent of the same user** (for example Planner asking Scout),
with `task_start(agent="<id>")`. It is opt-in and explicit:

- **Allowlist.** The agent's `delegation.targets` lists the agents it may use; empty (the default) means fresh workers only. Pick them
  in the agent editor (Tools tab, **Delegation** section, shown while the Delegate group is ticked) or in the yaml
  ([reference](../reference/agent-yaml.md#delegation)). A target must be another agent of the same user; there is no way to name another user's agent.
  If a target is renamed or deleted, the lists that name it follow (renamed) or drop it (deleted).
- **It runs as that agent.** Its own SOUL, model, provider and settings; `instructions` and `model` are ignored. It sees `goal` and `context`
  as a message that starts with `Task from <agent name> (<user>)`, and nothing else of the launching chat. Its tool groups are the ones the
  launcher grants in `tools` (only `web`) **that the target has itself**; the terminal only on backends both agents have, always on manual approvals.
  MCP services are never available to a task.
- **Where it lives.** The hidden worker chat is stored under the **target** agent (not the launcher) and the task card, the approval
  attribution and the Telegram status line show the target's name instead of "ephemeral".
- **Memory.** By default the task is excluded from learning and the target cannot write memory, like a fresh worker. With **Can write its own memory**
  (`remember: true` on that target) the worker chat is a hidden but ordinary chat of the target: Dream may review it, and the target keeps its own
  `memory_read`/`memory_edit`/`self` memory tools (never `soul_edit` or `skill_manage`) and writes to **its own** memory. Because memory
  files are not locked against concurrent edits across chats, such a task is refused while the target is replying in another chat
  (or already runs another memory-writing task), and the same applies to `task_send` follow-ups. The target gets only those of
  these memory groups that the **launching** agent holds itself (a launcher without `memory_edit` cannot hand a target write access to memory); `task_send`
  re-checks this and is refused when the launcher has lost a group the task was granted.
- **No loops.** Depth stays 1: a task never launches tasks. An agent cannot delegate to itself, and `A -> B` is refused while `B` is
  waiting on a task that `A` is running. Per-user and per-chat limits still apply, and the target may be busy in another chat (each chat has its own worker).

## Permission levels

Delegation follows the [permission levels](../architecture/permissions.md#what-is-enforced-step-2): the `delegate` tools exist from
`collaborator`, handing a task to another agent (`agent`) needs the launcher at `operator`, and a worker never outranks its
launcher: its level is the lower of its own (the target agent's, for an existing agent) and the launcher's, so the groups, terminal
backends and delegation targets above that level are dropped.

## Terminal and approvals

`task_start` takes `backends`: the [terminal backends](terminal.md#backends) to hand to the worker. Naming a backend grants the worker the
terminal tools (`terminal` and the `sandbox_*` tools) for those backends only; it is possible only if the agent has the `terminal` group
and the backend itself, so a worker never gets a backend its parent lacks.

- **Always `manual`.** Whatever the parent's mode for that backend (`off`, `smart` or `manual`, `local` included), every command the worker runs
  needs your approval. Hardline rules, dangerous-command detection and [protected paths](terminal.md#protected-paths-local-backend) apply unchanged:
  a protected path is refused on `local` in every case, without asking.
- **The card appears in the chat that launched the task**, not in the worker's hidden chat: in the web interface (live, and again after a reload),
  or as a Telegram message in the agent's Telegram chat. It says which task asks: *Task Check the logs (ephemeral) wants to run:*, followed by
  the command, backend, working directory and reason. Only the owner can answer; approving resumes the worker, denying makes it report the refusal.
  An approval is shown in the channel of the launching chat only: it is not relayed to the other channel.
- **At most 10 approvals per run, and the pause has a limit.** A worker that asks for an 11th approval in the same run gets it denied at once with a clear message; and the time spent waiting for you pauses the timeout for at most twice the task's timeout, after that the timeout runs even while a command waits.
- **The clock stops while you decide.** The task's 10-minute timeout does not run while a command waits for you (up to the limit above). An approval nobody answers is
  denied after the usual approval timeout (5 minutes by default), as for any chat.
- **Results are untrusted data.** What `task_result`, `task_wait` and `task_start(wait=true)` hand back to the agent, like the wake message, is wrapped in `<task_result untrusted="true">…</task_result>` with a note that it is worker data, not instructions; any tag-like text inside it is neutralised so a worker cannot close the block or fake another.
- **Worker chats are read-only.** The hidden chat of a task can be opened to read, but nobody (web, API) can write or edit in it: *this is an internal task chat: steer the task instead*. Use *Steer*, or `task_send` after it ends.
- **Awaiting approval.** A task card shows *Awaiting approval* while its worker waits for you, and Telegram `/tasks` says *awaiting approval*.
- **No unattended asking.** A worker run that nobody watches (not launched from a chat) never asks: `manual` and dangerous actions are
  denied. Tasks launched from a chat are always attended.
- **Still no memory.** A worker with the terminal stays out of Dream and indexing and never gets memory, self, folder-edit or `delegate` tools.

## Limits

Application defaults live in the `subagents:` block of [settings.yaml](../reference/settings-yaml.md#subagents); an agent's `delegation` block can
only lower them.

| Limit | Default |
| --- | --- |
| running tasks per chat / per user | 3 / 4 |
| launches in one turn | 5 |
| wall clock per task | 600 s (10 min) |
| input + output tokens per task (summed over every model call, `task_send` follow-ups restart the count) | 200 000 |
| result text returned to the agent | 8000 characters (the full text stays in the hidden chat) |
| follow-ups per task (`task_send`) | 5 |
| `task_wait` | 120 s at most |
| result-started turns in a row without a user message | 3 |
| depth | 1 |

The agent is told the numbers in the `task_start` description and in a short notice in its system prompt, and a limit error says which limit was hit and what
to do (`limit reached: 3 running task(s) in this chat; wait for one to finish (task_wait), then launch again`). A task that hits the wall clock or the token cap is
stopped and ends `timed_out` with its partial result; a user stopping the parent's reply cancels the task.

**Daily budget.** All your tasks together (every run, `task_send` follow-ups included) may use `subagents.daily_token_budget` input + output tokens per local day, 1 000 000 by default (`0` = unlimited). Once it is spent, `task_start` and `task_send` answer `error: daily task budget reached (<used>/<budget> tokens today); it resets at midnight` and nothing starts; tasks already running are **stopped** as soon as the budget is crossed (status *timed out*, reason `daily_budget`, partial result kept), and the check and the launch are one atomic step, so parallel launches cannot overshoot it. The Tasks panel shows today's usage. The budget is per user (User A and User B count separately) and is counted from the task records, not from your own chats.

**Unattended tasks (for scheduled jobs).** A task can also be started without any chat, by a program rather than by an agent: it runs with no one to approve anything, so manual and dangerous terminal actions are **denied**, `smart`-mode normal commands run, and protected paths are always refused. The same limits and the daily budget apply. Its result stays in the task record (visible in the Tasks panel, marked *scheduled*) or, when a target chat is given, arrives there as an ordinary task result. There is no scheduler, no screen to create jobs and no agent tool for this yet; the only entry point is the [admin CLI](admin-cli.md) (`python -m src.admin task run`), meant for testing.

**Provider limits and rate limits.** If the provider answers with a plan or rate limit (for example the 5-hour window of a Claude subscription, which workers share with
your own chats), the task ends `failed` with reason `rate_limit`, and **no new task starts for a few minutes**
(`subagents.rate_limit_cooldown_seconds`, 5 by default). A rate limit is never retried by the framework.

## Where the data lives

`data/<user_id>/.tasks/<task_id>.json`: one record per task (goal, status, reason, usage, capped result; no secrets), written under a lock. A running task also
holds `<task_id>.run.lock`; if the process dies, the next launch or start-up marks the orphan `failed` with reason `owner_lost` (nothing is resumed or re-run). Records are not
removed automatically yet.

## Security notes

The result of a worker is data written by another model, possibly after reading the web. It is returned with a note telling the agent to treat it as data, but the
risk of prompt injection through it remains: keep sensitive tools (terminal, folders with write access) in mind when you give an agent both `web` and `delegate`.
A worker never has more access than its parent and has no secrets, memory or files.

See also: the built-in `recall` skill page `delegation.md` (what agents read about this), [tools](../reference/tools.md#delegate), and the
[architecture note](../architecture/agents-and-users.md#subagent-tasks).

## Telegram

When a task is launched from a Telegram chat (or from the chat Telegram currently shows), the task appears like any other tool call: the `task_start(...)` line in the reply that launched it (shown while it runs, also for `wait=true`). That same reply message carries a **Stop** button (`Stop <title>`, one per running task) only while the task runs; the button disappears when the task ends, edited on the same message, and there are no separate "started" / "finished" messages. If the reply that launched it is long gone, nothing is added: use `/tasks`. When the results wake the parent, its reply is streamed into the private chat like any other reply (approvals included), without you writing anything; if you are in the middle of a reply, the wake waits behind it.

- `/tasks`: the tasks of the current chat (status, title, elapsed), with a Stop button per running task.
- `/task <id>`: status and result of a task (capped at 1500 characters).
- `/task <id> <text>`: corrects a running task; its current generation is interrupted, the partial reply is kept and your text is appended.

When a worker with a terminal grant needs approval, the bot sends it to the agent's Telegram chat with the usual **Approve** / **Deny** buttons and the
line *Task <title> (ephemeral) wants to run:*; the message closes with the outcome. Only the user the task belongs to can stop, correct or approve.

### Web and Telegram are two programs

The web app and the Telegram bot run as separate processes. A task runs in the one that runs the chat's reply, and both show the tasks of a chat. **Stop** and corrections work from either side: if the task runs in the other program, the command is queued and applied within a few seconds (Telegram says *queued*). Continuing a finished task (`task_send`) works from either. When a task ends, the message that wakes the agent is delivered by the program that owns the chat (a chat created in Telegram wakes in Telegram, a web chat in the web), even if the other program ran the task. If the program that ran a task stops, the next poll (a couple of seconds) marks the task `failed` / `owner_lost` and the owning program tells the agent. **Approvals are not forwarded**: a command that needs your approval is asked in the channel of the program that runs the task; the other channel only shows *awaiting approval (answer it in Web)* (or Telegram).
