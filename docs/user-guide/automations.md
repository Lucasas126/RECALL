# Automations

> **Status: the service and the agent tools are implemented; the web tab is the next step** (see the [backlog](../project/backlog.md)).
> You create and manage automations by asking an agent that holds the `automations` group; nothing in the web UI shows them yet.

An automation is something an agent does later, without you in the chat: "every morning at 8, summarise my notes and message me",
"on Friday at 9, remind me to call the bank", "when the build process ends, tell me how it went".

## What it is

A durable record per automation, stored in `data/<user>/.automations/<id>.json`:

- **who**: the owner (a RECALL user) and the agent that runs it. An agent never schedules another user's agent.
- **what**: the `instruction` the agent receives when it fires (up to 4000 characters).
- **when**: the `trigger`.
- the state: enabled or paused, `active`, `done` (a finished one-off) or `failed`, the next run, the last run, how many runs, and the last 20 outcomes.

At most 50 automations per user. Finished and failed records are removed after 30 days.

## Triggers

All times are in **Europe/Madrid** unless the trigger names another zone, and cron follows daylight saving time.

| kind | fields | meaning |
| --- | --- | --- |
| `at` | `at` (`2026-11-20T09:00`) | once, then the automation is `done`; must be in the future |
| `every` | `minutes` (5 to 44640) | repeatedly, counted from the last run |
| `cron` | `cron` (five fields, `30 8 * * mon-fri`) | on a cron schedule; not more often than every 5 minutes |
| `process_end` | `process` (`p1a2b3c4d`) | once, when that background process of the owner ends (a process you stopped does not count) |

Calendar and conversation events are not triggers.

## How it runs

When an automation is due, the service starts an **unattended task** for its agent (`TaskManager.launch_unattended`, the same
entry point as `python -m src.admin task run`). That means:

- The agent runs with **its own SOUL**, its web group and its terminal (memory stays read-only), in a hidden chat that is removed
  with the task. The result stays in the task record and shows in the task panel, marked as scheduled.
- **Nobody can approve**: manual and dangerous terminal actions are denied, normal commands in `smart` mode run, protected paths are refused.
- The task limits, token budget and the provider's rate-limit cooldown apply.
- **An automation's task may also message you.** Besides the groups above it keeps `notify` (`notify_telegram`) and `files_send`
  (`send_file`), but only if the agent itself holds those groups and its level allows them. No other task gets them: a worker started from a
  chat, or a plain scheduled task, still cannot write to your phone. The 30 messages an hour per agent apply.
- The agent's level is the ceiling: an automation never does more than its agent could.

## Guards

- At least **5 minutes** between runs of one automation, at most **6 runs an hour** and **48 a day** (a skipped run says why in its history).
- **After downtime** a missed run is made up **once**: an hourly automation that missed five hours runs one time when the server returns, not five.
- A run that cannot start (the provider's plan limit, task limits) is retried on the next check, up to 5 times, then skipped; the schedule goes on.
- If the agent was deleted, the automation is marked `failed` and disabled.
- Pausing and resuming never replays what was missed meanwhile.

The scheduler is a thread that checks every 30 seconds, started with the dream scheduler; its state is on disk, so it survives restarts.
The service is `AutomationService` (`src/service/automations.py`), reachable as `ChatService.automations`.

## Asking an agent

Give the agent the `automations` tool group (level `assistant`) and say it in plain words: "remind me on Friday at 9 to call the bank",
"every weekday at 8:30 tell me the three most important things in my notes", "when the build finishes, tell me how it went". The agent
calls `automation_create`; it writes the instruction for its future self, because it will not have this chat when it fires. The tools are
`automation_create`, `automation_list`, `automation_edit`, `automation_pause`, `automation_resume` and `automation_delete`
([tools reference](../reference/tools.md#automations)).

- An agent only sees and changes **its own** automations, for the user it works for. The owner and the agent are never parameters.
- The tools are refused inside an automation's own run, so a scheduled run cannot create more schedules.
- Pausing keeps the record; deleting removes it.

## Not built yet

- **A web tab** to see, pause, edit and delete automations and read their history.
- Sending a result to a chat (today it stays in the task record and the agent can message you with `notify_telegram`).
