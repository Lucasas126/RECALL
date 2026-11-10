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
from typing import Any, Callable, Literal
from uuid import uuid4

from src.utils.i18n import InputError, UserMessageError
from src.utils.logger import get_logger

logger = get_logger("terminal")

Risk = Literal["dangerous", "manual"]
DECISION_LOG_SIZE = 50
SESSION_GRANTS_PER_CHAT = 500
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
    scope: str = ""  # what an approval was granted for: once | session | agent; deny for an answered refusal; "" when not asked
    note: str = ""  # a problem that cut an "always" short (the approval stays a one-time one): text for the user

    @property
    def status(self) -> str:
        return "approved" if self.approved else "denied"


@dataclass
class Learn:
    """What 'approve for the session / always for this agent' remembers for one card.

    `signature` identifies the exact action for the session grant; `rules` are the narrow allow rules for 'agent'.
    `persist(rules)` writes them to the agent's configuration (raises when it cannot: the approval then stays a
    one-time one); `apply(rules)` adds them to the rule set of the running agent so its next call is not asked again.
    """

    signature: str
    rules: list = field(default_factory=list)  # list[ApprovalRule]
    persist: Callable[[list], None] | None = None
    apply: Callable[[list], None] | None = None


@dataclass
class Answer:
    """What `answer()` did: the status of the approval, the scope really granted and why a wider one was refused."""

    status: str  # approved | denied
    scope: str  # once | session | agent for an approval, deny for a refusal
    error: Exception | None = None


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
    options: tuple[str, ...] = ("once",)
    learn: Learn | None = None
    answering: threading.Lock = field(default_factory=threading.Lock)


class ApprovalBroker:
    def __init__(self, timeout_seconds: float = 300.0):
        self.timeout_seconds = timeout_seconds
        self._lock = threading.Lock()
        self._pending: dict[str, _Pending] = {}
        self._closed_ids: dict[str, tuple[str, Outcome]] = {}  # id -> (user_id, outcome), bounded
        self._log: dict[str, deque] = {}
        self._session_grants: dict[tuple[str, str, str], dict[str, None]] = {}  # (user, agent, chat) -> signatures
        self._shutdown = False

    # ---------------------------------------------------------------- asking
    def request(
        self, user_id: str, agent_id: str, chat_id: str, run: Any, tool: str, command: str, cwd: str,
        reason: str, risk: Risk, *, backend: str = "", timeout_seconds: float | None = None, task: dict | None = None,
        options: tuple[str, ...] = ("once",), learn: Learn | None = None,
    ) -> Outcome:
        """Emit an approval event and block until it is decided; `run` has `.emit(kind, data)` and `.cancellation`.

        `agent_id`/`chat_id` are the chat that shows the card: for a subagent task's run, its PARENT chat, with `task`
        (id, title, kind, worker) attributing the request. `options` are the approve scopes the card offers (`once`,
        `session`, `agent`; `learn` carries what the last two remember). A run may route its events elsewhere (`approval_emit`) and
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
            "backend": backend, "options": list(options),
        }
        if task:
            payload["task"] = dict(task)
        item = _Pending(approval_id, user_id, agent_id, chat_id, payload, run, options=tuple(options), learn=learn)
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
    def resolve(self, approval_id: str, user_id: str, decision: str, scope: str = "once") -> str:
        """The owner approves or denies; returns 'approved' or 'denied' (idempotent for a user's own answer)."""
        return self.answer(approval_id, user_id, decision, scope).status

    def answer(self, approval_id: str, user_id: str, decision: str, scope: str = "once") -> Answer:
        """Like `resolve`, with the scope: `once`, `session` (remember the exact action for this chat) or `agent`.

        A scope the card did not offer is an InputError. When writing the agent rule fails, the approval is granted
        for this one action only and the failure comes back in `Answer.error` (nothing is lost, nothing is widened).
        """
        if decision not in ("approve", "deny"):
            raise InputError("decision must be 'approve' or 'deny'")
        if scope not in ("once", "session", "agent"):
            raise InputError("scope must be 'once', 'session' or 'agent'")
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
            return Answer(outcome.status, outcome.scope)
        if item.user_id != user_id:
            raise ApprovalForbidden("this approval belongs to another user")
        if decision == "deny":
            outcome = Outcome(False, "denied by user", by_user=True, scope="deny")
            won = self._close(item, outcome)
            final = outcome if won else (item.outcome or outcome)
            return Answer(final.status, final.scope)
        if scope not in item.options:
            raise InputError("this approval does not offer the scope '{scope}'", scope=scope)
        error: Exception | None = None
        with item.answering:  # two answers at once must not both write a rule
            if item.outcome is not None:
                return Answer(item.outcome.status, item.outcome.scope)
            granted = scope
            if scope != "once" and item.learn is None:
                granted = "once"
            elif scope == "agent":
                learn = item.learn
                try:
                    if learn.persist is None:
                        raise RuntimeError("the agent's rules cannot be changed from here")
                    learn.persist(list(learn.rules))
                except Exception as exc:  # the approval stays a one-time one
                    logger.warning("could not remember approval %s for the agent: %s", approval_id, exc)
                    error, granted = exc, "once"
                else:
                    if learn.apply is not None:
                        _safe_apply(learn.apply, list(learn.rules))
            elif scope == "session":
                self._grant_session(item, item.learn.signature)
            outcome = Outcome(True, "approved", by_user=True, scope=granted)
            won = self._close(item, outcome)
        final = outcome if won else (item.outcome or outcome)
        return Answer(final.status, final.scope, error)

    # ---------------------------------------------------------------- session grants
    def _grant_session(self, item: _Pending, signature: str) -> None:
        with self._lock:
            grants = self._session_grants.setdefault((item.user_id, item.agent_id, item.chat_id), {})
            grants.pop(signature, None)
            grants[signature] = None
            while len(grants) > SESSION_GRANTS_PER_CHAT:
                grants.pop(next(iter(grants)))

    def session_allows(self, user_id: str, agent_id: str, chat_id: str, signature: str) -> bool:
        """True when the user approved exactly this action 'for this session' in this chat."""
        with self._lock:
            return signature in self._session_grants.get((user_id, agent_id, chat_id), ())

    def forget_chat(self, user_id: str, agent_id: str, chat_id: str) -> None:
        """The chat is gone: so are its session grants."""
        with self._lock:
            self._session_grants.pop((user_id, agent_id, chat_id), None)

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
                "scope": outcome.scope,
            })
        try:  # tell every client before the tool thread resumes
            _emitter(item.run)("approval_resolved", {
                "id": item.id, "decision": outcome.status, "reason": outcome.reason, "scope": outcome.scope,
            })
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


def _safe_apply(callback, rules: list) -> None:
    try:
        callback(rules)
    except Exception:  # the rule is saved; the running agent picks it up with its next rebuild
        logger.exception("could not add the learned rules to the running agent")


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
