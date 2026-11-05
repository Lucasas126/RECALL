# Tasks across the web and Telegram processes

RECALL runs the web app and the Telegram bot as two separate programs that share only files. This page says what that means for tasks you launch or the user manages.

## Who does what

- The program that runs your turn **executes** the task you launch (it holds the task's run lock). A chat created in Telegram is driven by the Telegram program, a web chat by the web program.
- Both programs read the same task store, so `task_list`, the web task cards and Telegram `/tasks` show the tasks of the chat wherever they run.
- The wake message that tells you a task ended is delivered by the program that owns the chat, even when the other program executed the task. It can take a couple of seconds.

## Stop, steer, send

- `task_stop` and `task_steer` on a task that runs in the other program do not fail: they return `status: queued`. The executor applies the command within about a second and records whether it was `applied` or `rejected` (for example the task had just ended). Check with `task_list` or `task_result` before assuming it worked.
- The same holds when the user presses Stop or sends a correction from the other channel.
- `task_send` on a finished task always runs in the program that handles the call; a send to a task that is still running is rejected (use `task_steer`).

## Approvals are not forwarded

A worker with a terminal grant asks for approval in the channel of the program that executes it, never in the other one. The other channel only shows *awaiting approval (answer it in Web|Telegram)* on the task card. If the user is not in that channel, the request times out and is denied.

## When the executor dies

If the program that ran a task stops or crashes, the other program notices (its poll runs every ~2 s): the task becomes `failed` with reason `owner_lost` and the owning program wakes you with that result. Nothing is resumed or re-run; the worker's partial chat stays readable. A wake claimed by a program that then died is taken over after 60 s, and a result is never delivered twice.

## For admins

`ChatService(origins=...)` declares which chat origins a process drives (`web.py` -> web, `run_telegram.py` -> Telegram); the poller (`ChatService.start_task_poller`, started with the dream scheduler) and the commands inbox (`data/<user>/.tasks/<id>.cmd.jsonl`) live in `src/service/tasks.py`. If only one program is running, chats of the other origin get their wakes when it starts.
