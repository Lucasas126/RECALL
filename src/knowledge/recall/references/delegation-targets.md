# Delegating to another agent

Besides fresh workers you can hand a task to another agent of the same user with `task_start(agent="<id>")`.

## When it is allowed

- Only agents listed in your `delegation.targets` (the agent editor, Tools tab, Delegation section). The `agent` parameter only
  appears when you have targets. Another user's agents can never be named.
- Not yourself, and not an agent that is currently waiting on a task you are running (no `A -> B -> A` loops). Depth is 1: the
  target cannot launch tasks while working for you.
- The usual limits apply (3 running tasks per chat, 4 per user, 10 minutes, the token budget).
- A backend granted to a target must be set up the same for both agents (same workdir and approval mode), else it is refused naming the backend.

## How the target works

- It is that agent: its own SOUL, model, provider and settings. `instructions` and `model` are ignored; write what it needs in
  `goal` and `context`. Its message starts with `Task from <your name> (<user>)` and it sees nothing else of your chat.
- Tools: only the groups you grant in `tools` that the target has itself. Terminal: only backends both of you have, always with
  manual approvals; its approval cards show in your chat, attributed to the task. No MCP.
- Its worker chat is hidden and stored under the target agent. Cards and Telegram lines show its name.

## Memory (`remember`)

- Default (`remember: false`): like a fresh worker, the task is excluded from learning and the target cannot write memory.
- `remember: true` on that target ("Can write its own memory"): the chat is a hidden but ordinary chat of the target, Dream may
  review it, and the target keeps `memory_read`, `memory_edit` and its `self` memory tools (never `soul_edit` or `skill_manage`),
  so it records what it learned in **its own** memory.
- Memory files are not locked across chats, so a remember task (and a `task_send` follow-up on it) is refused while the target is
  replying in another chat or already runs another remember task. Wait and try again; the error says so.

## Good uses

- A specialist that already knows the topic (for example a server administrator agent) answering or doing a bounded job.
- Asking for its opinion with its own memory, then reporting to the user.

Not for chatting back and forth: use one task with a clear goal, then `task_send` to continue it with its full context.

Code: `src/service/tasks.py` (launch and grants), `src/agent/agent.py` (worker build), `src/config/models.py` (`DelegationTarget`).
