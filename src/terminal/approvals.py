"""Approval broker: a tool thread asks, a person answers (web card, Telegram button), the thread resumes.

`request()` emits an `approval` event through the run's event stream and BLOCKS the calling tool thread until
the owner resolves it, the timeout passes (deny), the chat run is cancelled/interrupted/discarded (deny) or the
service shuts down (deny). Only the chat's owner may resolve. Resolving twice is safe.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import uuid4

from src.utils.i18n import InputError, UserMessageError
from src.utils.logger import get_logger

logger = get_logger("terminal")

Risk = Literal["dangerous", "manual"]
DECISION_LOG_SIZE = 50
_RESOLVED_KEEP = 500
_POLL_SECONDS = 0.2


class ApprovalNotFound(UserMessageError):
    """Unknown, expired or already closed approval id."""

    code = "unknown_approval"


class ApprovalForbidden(UserMessageError):
    """Someone other than the chat's owner tried to resolve an approval."""

    code = "approval_forbidden"


@dataclass
class Outcome:
    approved: bool
    reason: str  # 'approved' | 'denied by user' | 'timed out' | 'cancelled' | 'service shutdown'
    by_user: bool = False
    approval_id: str = ""  # the card that was answered; empty when nobody was asked (a cap, a shutdown)

    @property
    def status(self) -> str:
        return "approved" if self.approved else "denied"


@dataclass
class _Pending:
    id: str
    user_id: str
    agent_id: str
    chat_id: str
    payload: dict
    run: Any
    event: threading.Event = field(default_factory=threading.Event)
    outcome: Outcome | None = None


class ApprovalBroker:
    def __init__(self, timeout_seconds: float = 300.0):
        self.timeout_seconds = timeout_seconds
        self._lock = threading.Lock()
        self._pending: dict[str, _Pending] = {}
        self._closed_ids: dict[str, tuple[str, Outcome]] = {}  # id -> (user_id, outcome), bounded
        self._log: dict[str, deque] = {}
        self._shutdown = False

    # ---------------------------------------------------------------- asking
    def request(
        self, user_id: str, agent_id: str, chat_id: str, run: Any, tool: str, command: str, cwd: str,
        reason: str, risk: Risk, *, backend: str = "", timeout_seconds: float | None = None, task: dict | None = None,
    ) -> Outcome:
        """Emit an approval event and block until it is decided; `run` has `.emit(kind, data)` and `.cancellation`.

        `agent_id`/`chat_id` are the chat that shows the card: for a subagent task's run, its PARENT chat, with `task`
        (id, title, kind, worker) attributing the request. A run may route its events elsewhere (`approval_emit`) and
        be told while it waits (`approval_wait`), which a task uses to pause its wall-clock timeout.
        """
        timeout = self.timeout_seconds if timeout_seconds is None else timeout_seconds
        gate = getattr(run, "approval_gate", None)
        if gate is not None and (refusal := gate()):  # e.g. a task past its approval cap: denied, nobody is asked
            return Outcome(False, refusal)
        approval_id = uuid4().hex[:12]
        now = time.time()
        payload = {
            "id": approval_id, "tool": tool, "command": command, "cwd": cwd, "reason": reason, "risk": risk,
            "timeout_seconds": int(timeout), "expires_at": now + timeout, "chat_id": chat_id, "agent_id": agent_id,
            "backend": backend,
        }
        if task:
            payload["task"] = dict(task)
        item = _Pending(approval_id, user_id, agent_id, chat_id, payload, run)
        with self._lock:
            if self._shutdown:
                return Outcome(False, "service shutdown")
            self._pending[approval_id] = item
        waiting = getattr(run, "approval_wait", None)
        if waiting is not None:
            _safe(waiting, True)
        try:
            try:
                _emitter(run)("approval", dict(payload))
            except Exception:  # an unreachable client is the same as nobody answering
                logger.exception("could not deliver approval %s", approval_id)
            deadline = time.monotonic() + timeout
            cancellation = getattr(run, "cancellation", None)
            while not item.event.wait(_POLL_SECONDS):
                if cancellation is not None and _is_cancelled(cancellation):
                    self._close(item, Outcome(False, "cancelled"))
                elif time.monotonic() >= deadline:
                    self._close(item, Outcome(False, "timed out"))
        finally:
            if waiting is not None:
                _safe(waiting, False)
        return item.outcome or Outcome(False, "cancelled")

    # ---------------------------------------------------------------- answering
    def resolve(self, approval_id: str, user_id: str, decision: str) -> str:
        """The owner approves or denies; returns 'approved' or 'denied' (idempotent for a user's own answer)."""
        if decision not in ("approve", "deny"):
            raise InputError("decision must be 'approve' or 'deny'")
        with self._lock:
            item = self._pending.get(approval_id)
            closed = self._closed_ids.get(approval_id)
        if item is None:
            if closed is None:
                raise ApprovalNotFound("unknown or expired approval: {approval_id}", approval_id=approval_id)
            owner, outcome = closed
            if owner != user_id:
                raise ApprovalForbidden("this approval belongs to another user")
            if not outcome.by_user:
                raise ApprovalNotFound("unknown or expired approval: {approval_id}", approval_id=approval_id)
            return outcome.status
        if item.user_id != user_id:
            raise ApprovalForbidden("this approval belongs to another user")
        if decision == "approve":
            outcome = Outcome(True, "approved", by_user=True)
        else:
            outcome = Outcome(False, "denied by user", by_user=True)
        won = self._close(item, outcome)
        return (outcome if won else (item.outcome or outcome)).status

    def _close(self, item: _Pending, outcome: Outcome) -> bool:
        with self._lock:
            if item.outcome is not None:
                return False
            outcome.approval_id = item.id
            item.outcome = outcome
            self._pending.pop(item.id, None)
            self._closed_ids[item.id] = (item.user_id, outcome)
            while len(self._closed_ids) > _RESOLVED_KEEP:
                self._closed_ids.pop(next(iter(self._closed_ids)))
            log = self._log.setdefault(item.user_id, deque(maxlen=DECISION_LOG_SIZE))
            log.append({
                "id": item.id, "ts": time.time(), "agent_id": item.agent_id, "chat_id": item.chat_id,
                "tool": item.payload["tool"], "backend": item.payload["backend"], "command": item.payload["command"][:500],
                "risk": item.payload["risk"], "decision": outcome.status, "reason": outcome.reason,
            })
        try:  # tell every client before the tool thread resumes
            _emitter(item.run)("approval_resolved", {"id": item.id, "decision": outcome.status, "reason": outcome.reason})
        except Exception:
            logger.exception("could not deliver the resolution of approval %s", item.id)
        item.event.set()
        return True

    # ---------------------------------------------------------------- inspection
    def pending(self, user_id: str, agent_id: str, chat_id: str) -> list[dict]:
        with self._lock:
            return [dict(p.payload) for p in self._pending.values()
                    if p.user_id == user_id and p.agent_id == agent_id and p.chat_id == chat_id]

    def pending_for_task(self, user_id: str, task_id: str) -> int:
        """How many approvals a subagent task is waiting on right now."""
        with self._lock:
            return sum(1 for p in self._pending.values()
                       if p.user_id == user_id and (p.payload.get("task") or {}).get("id") == task_id)

    def get(self, approval_id: str) -> dict | None:
        with self._lock:
            item = self._pending.get(approval_id)
            return dict(item.payload) if item else None

    def recent_decisions(self, user_id: str) -> list[dict]:
        with self._lock:
            return list(self._log.get(user_id, ()))

    def close(self) -> None:
        """Service shutdown: every waiting tool is denied."""
        with self._lock:
            self._shutdown = True
            items = list(self._pending.values())
        for item in items:
            self._close(item, Outcome(False, "service shutdown"))


def _emitter(run: Any):
    """Where a run's approval events go: its `approval_emit` (a task's parent chat) or its own stream."""
    return getattr(run, "approval_emit", None) or run.emit


def _safe(callback, value: bool) -> None:
    try:
        callback(value)
    except Exception:
        logger.exception("approval wait callback failed")


def _is_cancelled(token: Any) -> bool:
    from src.providers.cancellation import GenerationCancelled

    try:
        token.raise_if_cancelled()
    except GenerationCancelled:
        return True
    return False
