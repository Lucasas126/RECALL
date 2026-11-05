# Delegating tasks to a worker

> **Status: phase 1 of the [subagents design](../project/design-subagents.md).** An agent can hand one task to a worker and wait for the
> result. Tasks that run while the agent keeps talking, results that wake the agent, listing, steering and stopping tasks,
> terminal access for workers, other agents as workers and Telegram/web task cards are planned and **not built yet**.

The `delegate` tool group gives an agent one tool, `task_start`. The agent writes a self-contained goal, the context the worker needs
and the worker's own system prompt (`instructions`), and `task_start` runs a **worker** (a fresh, short-lived agent) in a hidden chat
and returns its result as the tool result. The agent then tells you what matters. It is useful for work that is big and independent:
reading many pages, researching one question in depth, drafting alternatives.

## Turning it on

Delegation is an explicit opt-in per agent, like folders and the terminal: add the **Delegate subtasks** group in the agent's
**Tools** tab, or `delegate` to `chat_config.tool_groups` in the agent yaml. The group gets default settings; the optional `delegation`
block of the [agent yaml](../reference/agent-yaml.md#delegation) tunes or switches it off (`enabled: false`). Nothing changes for agents
without the group.

## What a worker is

- **No inherited persona.** The agent writes the worker's system prompt through `instructions`; the worker does not get the agent's SOUL, memory,
  skills, files, MCP services or the terminal, and it cannot ask you questions.
- **Same provider and model as the chat that launched it**, unless the agent names another model (checked like a chat's own model choice,
  including the zero-data-retention rule). Reasoning effort follows the launching chat too.
- **Tools only by grant.** By default none. The agent may grant `web` (web search), and only if it has the group itself and `delegation.allowed_groups`
  allows it. Memory, folders, the terminal, MCP and `delegate` can never be granted, so a worker cannot do more than its parent and cannot launch tasks (depth 1).
- **A hidden chat.** The worker's conversation is a chat file of the launching agent, marked as a task chat: it is not in the chat list, is never the
  "latest chat", is not pruned while empty, is not reviewed by Dream and not indexed, and is removed after the disposable-chat retention
  period. A disposable chat cannot launch tasks.

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
| depth | 1 |

The agent is told the numbers in the `task_start` description and in a short notice in its system prompt, and a limit error says which limit was hit and what
to do (`limit reached: 3 running task(s) in this chat; wait for one to finish, then launch again`). A task that hits the wall clock or the token cap is
stopped and ends `timed_out` with its partial result; a user stopping the parent's reply cancels the task.

**Provider limits and rate limits.** If the provider answers with a plan or rate limit (for example the 5-hour window of a Claude subscription, which workers share with
your own chats), the task ends `failed` with reason `rate_limit`, and **no new task starts for a few minutes**
(`subagents.rate_limit_cooldown_seconds`, 5 by default). A rate limit is never retried by the framework.

## Where the data lives

`data/<user_id>/.tasks/<task_id>.json`: one record per task (goal, status, reason, usage, capped result; no secrets), written under a lock. A running task also
holds `<task_id>.run.lock`; if the process dies, the next launch marks the orphan `failed` with reason `owner_lost` (nothing is resumed or re-run). Records are not
removed automatically yet.

## Security notes

The result of a worker is data written by another model, possibly after reading the web. It is returned with a note telling the agent to treat it as data, but the
risk of prompt injection through it remains: keep sensitive tools (terminal, folders with write access) in mind when you give an agent both `web` and `delegate`.
A worker never has more access than its parent and has no secrets, memory or files.

See also: the built-in `recall` skill page `delegation.md` (what agents read about this), [tools](../reference/tools.md#delegate), and the
[architecture note](../architecture/agents-and-users.md#subagent-tasks).
