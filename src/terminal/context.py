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
    interactive: bool = True  # a person is attached and can answer an approval request
    tasks: Any = None  # TaskManager: what the `delegate` tools launch tasks through
    task_launches: int = 0  # tasks launched by this run (one parent turn), against max_launches_per_turn


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
