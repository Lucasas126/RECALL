"""Administrator command to start an unattended subagent task (phase 7 of the subagents design; a testing entry point).

    python -m src.admin task run --user U --agent A --goal "..." [--context C] [--instructions I]
                                 [--notify AGENT:CHAT] [--wait]

The task runs WITHOUT a parent run, as one of the user's agents (or, with `--instructions`, as an ephemeral worker on
that agent's model): manual and dangerous terminal actions are denied because nobody can answer, `smart` non-dangerous
ones are allowed, protected paths are always refused. The usual limits apply. It prints the
task id; with `--wait` it blocks until the task ends and prints the result. The task executes inside this command's
process, so without `--wait` it stops when the command exits (a scheduler running in a service process will not have
that restriction). With `--notify AGENT:CHAT` the result is delivered to that chat as a wake, as for a normal task;
otherwise it stays in the task record and in the web task panel.
"""

from __future__ import annotations

import sys
from time import monotonic, sleep

from src.admin.users import AdminError, check_user_id
from src.agent.toolsets.delegate import TaskError
from src.service.tasks import FINAL_STATES
from src.utils.i18n import InputError, error_message

WAKE_WAIT_SECONDS = 60  # `--notify --wait`: how long to wait for the result to reach the chat


def _make_service():
    from src.service.chat import ChatService

    return ChatService()


def add_parsers(sub) -> None:
    task = sub.add_parser("task", help="start an unattended subagent task (testing entry point for scheduled jobs)")
    tsub = task.add_subparsers(dest="task_command", required=True)
    run = tsub.add_parser("run", help="run a task with no parent run and print its id (and, with --wait, its result)")
    run.add_argument("--user", required=True, help="RECALL user id")
    run.add_argument("--agent", required=True, help="the user's agent that runs the task")
    run.add_argument("--goal", required=True, help="what the task must achieve")
    run.add_argument("--context", default="", help="facts the worker needs")
    run.add_argument("--instructions", default="",
                     help="run an ephemeral worker with this system prompt instead of the agent itself")
    run.add_argument("--notify", default="", metavar="AGENT:CHAT",
                     help="deliver the result to this chat of the user as a wake (default: only keep it in the task panel)")
    run.add_argument("--wait", action="store_true", help="wait for the task to end and print the result")


def _notify(value: str) -> tuple[str, str] | None:
    if not value:
        return None
    agent, _, chat = value.partition(":")
    if not agent or not chat:
        raise AdminError("--notify needs AGENT:CHAT")
    return agent, chat


def task_run(service, user_id: str, agent_id: str, goal: str, *, context: str = "", instructions: str = "",
             notify: tuple[str, str] | None = None, wait: bool = False) -> int:
    check_user_id(user_id)
    manager = service.tasks
    try:
        live = manager.launch_unattended(
            user_id, agent_id, goal, context, trigger="admin", notify_chat=notify, instructions=instructions,
        )
    except TaskError as exc:
        raise AdminError(str(exc)) from exc
    print(f"task: {live.task_id}")
    if not wait:
        print("running inside this command: without --wait it stops when the command exits; "
              "follow it in the web Tasks panel while a service is the executor.", file=sys.stderr)
        return 0
    try:
        record = manager.wait_for(live)
    except KeyboardInterrupt:
        manager.stop(user_id, live.task_id)
        live.done.wait(10)
        record = manager.store.get(user_id, live.task_id)
    record = record or {}
    if notify is not None and record.get("status") in FINAL_STATES:  # let the wake reach the chat before the process ends
        deadline = monotonic() + WAKE_WAIT_SECONDS
        while monotonic() < deadline and (manager.store.get(user_id, live.task_id) or {}).get("wake", {}).get("state") in ("pending", "claimed", "held"):
            sleep(0.2)
    usage = record.get("usage") or {}
    status = record.get("status", "unknown")
    reason = f" ({record['reason']})" if record.get("reason") else ""
    print(f"status: {status}{reason}")
    print(f"tokens: {(usage.get('in') or 0) + (usage.get('out') or 0)}")
    if (record.get("error") or {}).get("message"):
        print(f"error: {record['error']['message']}", file=sys.stderr)
    text = (record.get("result") or {}).get("text")
    if text:
        print("result:\n" + text)
    return 0 if status == "done" else 1


def run(args, service=None) -> int:
    """Run `task run`; prints `error: ...` to stderr and returns 1 on any failure."""
    from src.service.chat import ServiceError

    try:
        notify = _notify(args.notify)
        service = service or _make_service()
        return task_run(
            service, args.user, args.agent, args.goal, context=args.context, instructions=args.instructions,
            notify=notify, wait=args.wait,
        )
    except AdminError as exc:
        print(f"error: {exc}", file=sys.stderr)
    except (ServiceError, InputError) as exc:
        print(f"error: {error_message('en', exc)}", file=sys.stderr)
    return 1
