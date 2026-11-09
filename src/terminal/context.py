"""The run a terminal tool is executing in: who is chatting, how to reach them, how to tell if they left.

ChatService binds a `RunContext` around each chat reply (in the worker thread that runs the tools). Code that
runs without one (dream sessions, scripts) is *unattended*: it can never ask a person for approval.
"""

from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable, Iterator

from src.providers.cancellation import CancellationToken


@dataclass
class RunContext:
    user_id: str
    agent_id: str
    chat_id: str
    broker: Any  # ApprovalBroker
    emit: Callable[[str, dict], None]  # puts a ChatEvent of that kind (approval, approval_resolved) in the run's stream
    cancellation: CancellationToken | None = None
    # a person is attached and can answer an approval request; False unless the chat path says so explicitly
    interactive: bool = False
    tasks: Any = None  # TaskManager: what the `delegate` tools launch tasks through
    task_launches: int = 0  # tasks launched by this run (one parent turn), against max_launches_per_turn
    # a subagent task's run asks its PARENT chat (agent id, chat id) for approvals, so the cards appear where the user is
    approval_target: tuple[str, str] | None = None
    task: dict | None = None  # attribution of that run's approvals: {"id", "title", "kind" (ephemeral|agent), "worker"}
    # where the approval/approval_resolved events go instead of `emit` (the task manager's events for the parent chat)
    approval_emit: Callable[[str, dict], None] | None = None
    # called with True when the run starts waiting for an approval and False when it stops (pauses a task's timeout)
    approval_wait: Callable[[bool], None] | None = None
    # called before each approval request; returns a reason to DENY it without asking anyone (a task's approval cap)
    approval_gate: Callable[[], str | None] | None = None


_current: ContextVar[RunContext | None] = ContextVar("recall_terminal_run", default=None)


def current_run() -> RunContext | None:
    return _current.get()


@contextmanager
def run_context(ctx: RunContext) -> Iterator[RunContext]:
    token = _current.set(ctx)
    try:
        yield ctx
    finally:
        _current.reset(token)
