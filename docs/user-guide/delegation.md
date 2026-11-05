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
- **Stop, Steer, Open chat.** *Stop* ends a running task and keeps its partial result. *Steer* opens a small field: what you type is sent into the running worker as a correction (its current reply is interrupted and kept, your text is appended and it continues), the same as the agent's `task_steer`. *Open chat* shows the worker's own conversation in a dialog marked as internal and read-only; it is never in your chat list.
- **Task result bubble.** When a task ends, the result that reaches the agent appears as a compact, collapsed "Task result" note listing the task titles, not as a message of yours. The agent's reply to it follows as a normal message and streams live if the chat is open, also after a reload.
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
- **Same provider and model as the chat that launched it**, unless the agent names another model (checked like a chat's own model choice,
  including the zero-data-retention rule). Reasoning effort follows the launching chat too.
- **Tools only by grant.** By default none. The agent may grant `web` (web search), and only if it has the group itself and `delegation.allowed_groups`
  allows it. Memory, folders, MCP and `delegate` can never be granted, and the terminal only through `backends`, so a worker cannot do more than its parent and cannot launch tasks (depth 1).
- **A hidden chat.** The worker's conversation is a chat file of the launching agent, marked as a task chat: it is not in the chat list, is never the
  "latest chat", is not pruned while empty, is not reviewed by Dream and not indexed, and is removed after the disposable-chat retention
  period. A disposable chat cannot launch tasks.

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
- **The clock stops while you decide.** The task's 10-minute timeout does not run while a command waits for you. An approval nobody answers is
  denied after the usual approval timeout (5 minutes by default), as for any chat.
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
| input + output tokens per task | 200 000 |
| result text returned to the agent | 8000 characters (the full text stays in the hidden chat) |
| follow-ups per task (`task_send`) | 5 |
| `task_wait` | 120 s at most |
| result-started turns in a row without a user message | 3 |
| depth | 1 |

The agent is told the numbers in the `task_start` description and in a short notice in its system prompt, and a limit error says which limit was hit and what
to do (`limit reached: 3 running task(s) in this chat; wait for one to finish (task_wait), then launch again`). A task that hits the wall clock or the token cap is
stopped and ends `timed_out` with its partial result; a user stopping the parent's reply cancels the task.

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

When a task is launched from a Telegram chat (or from the chat Telegram currently shows), the bot sends one short line when it starts (with a **Stop** button) and one when it ends: status, title, agent and elapsed time. If the chat is not the current one, the line and the reply name the agent and the chat. When the results wake the parent, its reply is streamed into the private chat like any other reply (approvals included), without you writing anything; if you are in the middle of a reply, the wake waits behind it.

- `/tasks`: the tasks of the current chat (status, title, elapsed), with a Stop button per running task.
- `/task <id>`: status and result of a task (capped at 1500 characters).
- `/task <id> <text>`: corrects a running task; its current generation is interrupted, the partial reply is kept and your text is appended.

When a worker with a terminal grant needs approval, the bot sends it to the agent's Telegram chat with the usual **Approve** / **Deny** buttons and the
line *Task <title> (ephemeral) wants to run:*; the message closes with the outcome. Only the user the task belongs to can stop, correct or approve. The bot only sees tasks that run in its own process, so a task started from the web UI is managed there.
