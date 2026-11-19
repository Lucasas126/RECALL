"""One agent's terminal: the backend, the approval policy and the audit trail around every action."""

from __future__ import annotations

import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from src.config.models import AgentTerminalConfig, TerminalSettings
from src.providers.cancellation import GenerationCancelled
from src.terminal.audit import audit_writable, write_audit
from src.terminal.backends import Backend, LocalBackend, Result
from src.terminal.context import current_run
from src.terminal.policy import Assessment, assess
from src.terminal.protected import ProtectedPaths, build_protected
from src.terminal.approvals import Learn
from src.terminal.rules import Action, ApprovalRule, Decision, RuleSet, action_signature, evaluate, offered_scopes, rule_key, rules_for_action
from src.utils.logger import get_logger

logger = get_logger("terminal")
MAX_COMMAND_CHARS = 20_000

# file tools that change files (kind `file_write`); `off` never lets them run unattended
WRITE_TOOLS = frozenset({"sandbox_write", "sandbox_patch"})
READ_TOOLS = frozenset({"sandbox_read", "sandbox_search", "send_file", "view_image"})
AUDIT_FAILED = "error: the action was not run because the terminal audit trail could not be written. Tell the user."

DENIED_HINT = "Do not retry this action; tell the user what you wanted to do and why, and let them decide."
PROTECTED_NOTICE = (
    "Denied: protected path: {why}. SSH keys, secrets files, .env files, login credentials, MCP credentials and process "
    "environments are off limits to agents on the local backend in every approval mode, and so is writing audit trails, "
    "user/agent/settings configuration files and known_hosts files. No one can approve access to them from here. "
    "Do not try another way to reach them (other tools, paths, globs, scripts). "
)


def tool_kind(tool: str) -> str:
    """The rule kind of a terminal tool: `terminal`, `file_read` (sandbox_read/search) or `file_write` (write/patch)."""
    return "file_write" if tool in WRITE_TOOLS else "file_read" if tool in READ_TOOLS else "terminal"


@dataclass(frozen=True)
class Ruling:
    """The outcome of `decide`: whether to run, what to tell the model, the audit label and the rule that decided."""

    allowed: bool
    text: str
    label: str  # 'approved', 'auto:off', 'denied:unattended', ... (the audit `decision`)
    decision: Decision | None = None  # None for the fixed floor and for the same-operation second step
    layer: str | None = None
    rule: str | None = None
    approval_id: str = ""
    scope: str = ""  # once | session | agent when the user approved, deny when the user was asked and refused, else ''


class TerminalSession:
    """Runs commands through a backend after the approval policy allowed them.

    `unattended` forces the no-client behaviour (dream sessions); otherwise the active chat run decides: no
    run context, or one flagged non-interactive, is unattended too.
    """

    def __init__(
        self, backend: Backend, config: AgentTerminalConfig, settings: TerminalSettings, *,
        user_id: str, agent_id: str, name: str, audit_file: Path | None = None, unattended: bool = False,
        local: bool | None = None, rules: RuleSet | None = None, kind: str | None = None,
        rules_provider: Callable[[], RuleSet] | None = None,
    ):
        self.backend = backend
        self.name = name  # the key in the user's terminal_backends
        self.config = config
        self.policy = config.backends[name]  # this backend's workdir and approval mode
        self.settings = settings
        self.user_id = user_id
        self.agent_id = agent_id
        self.audit_file = audit_file
        self.unattended = unattended
        # the protected-path rules apply to the local backend only (ssh backends are other machines)
        self.local = isinstance(backend, LocalBackend) if local is None else local
        # the approval rules (installation, user and agent layers); none = the legacy per-backend mode alone
        self._rules = rules if rules is not None else RuleSet()
        # returns the current rule set (it keeps the last good one when the files cannot be read); none = `rules` is fixed
        self._rules_provider = rules_provider
        self._learned: tuple[ApprovalRule, ...] = ()  # learned rules not yet seen in a reloaded rule set
        self._learned_base: RuleSet | None = None
        self.kind = kind or ("local" if self.local else "machine")  # sandbox | machine | local
        self.shorthand = config.shorthand(name)  # the explicit `approvals` mode of this backend, if the config sets one

    @property
    def rules(self) -> RuleSet:
        """The approval rules in force now: the provider's current set (when there is one) plus rules learned since."""
        if self._rules_provider is None:
            return self._rules
        fresh = self._rules_provider()
        learned, base = self._learned, self._learned_base
        if learned and fresh is not base:  # the files were reloaded after the rules were persisted: they hold them now
            self._learned, self._learned_base = (), None
            learned = ()
        return self._with_learned(fresh, learned)

    @rules.setter
    def rules(self, value: RuleSet) -> None:
        self._rules = value

    @staticmethod
    def _with_learned(current: RuleSet, learned: tuple[ApprovalRule, ...]) -> RuleSet:
        known = {rule_key(rule) for rule in current.layer("agent").rules}
        for rule in learned:
            if rule_key(rule) not in known:
                current = current.with_rule("agent", rule)
                known.add(rule_key(rule))
        return current

    def add_rules(self, rules: list[ApprovalRule]) -> None:
        """Append rules to the agent layer of the running rule set (a learned 'always for this agent'); skips duplicates."""
        if self._rules_provider is None:
            self._rules = self._with_learned(self._rules, tuple(rules))  # one assignment: a concurrent decision sees the old or the new set
            return
        base = self._rules_provider()
        known = {rule_key(rule) for rule in self._learned}
        added = tuple(rule for rule in rules if rule_key(rule) not in known)
        self._learned_base = base
        self._learned = (*self._learned, *added)

    # ------------------------------------------------------------------ protected paths
    def protected(self) -> ProtectedPaths | None:
        """The protected set right now (recomputed on every call), or None on a backend that is not local."""
        return build_protected(self.settings.protected_paths) if self.local else None

    def local_cwd(self, cwd: str | None = None) -> str:
        """The directory a local command really runs in (mirrors LocalBackend.run)."""
        base = Path(self.workdir).expanduser()
        target = Path(cwd).expanduser() if cwd and cwd != "~/workspace" else base
        return str(target if target.is_absolute() else base / target)

    @property
    def workdir(self) -> str:
        return self.policy.workdir

    # ------------------------------------------------------------------ limits
    @property
    def output_limit(self) -> int:
        return min(self.config.max_output_chars, self.settings.max_output_chars)

    def timeout_for(self, requested: int | float | None) -> float:
        default = min(self.config.timeout_for_backend(self.name), self.settings.max_timeout_seconds)
        if requested is None:
            return float(default)
        try:
            value = float(requested)
        except (TypeError, ValueError):
            return float(default)
        return float(max(1.0, min(value, self.settings.max_timeout_seconds)))

    # ------------------------------------------------------------------ policy
    def decide(
        self, tool: str, display: str, assessment: Assessment, cwd: str, detail: str = "", *, command: str = "",
        path: str = "",
    ) -> Ruling:
        """Allow, ask or deny one action: the fixed floor first, then the approval rules (see `src.terminal.rules`)."""
        if assessment.level == "protected":  # before every rule and before the unattended handling
            text = PROTECTED_NOTICE.format(why=assessment.reason or "keys and secrets") + DENIED_HINT
            return Ruling(False, text, "denied:protected", None, "floor", "protected")
        if assessment.level == "hardline":
            why = assessment.reason or "matches the hardline blocklist"
            return Ruling(False, (
                f"Blocked: this action ({why}) is on the hardline blocklist and can never run on this backend, "
                f"whatever the approval mode. {DENIED_HINT}"
            ), "hardline", None, "floor", "hardline")
        run = current_run()
        dangerous = assessment.level == "dangerous"
        attended = not self.unattended and run is not None and run.interactive
        kind = tool_kind(tool)
        action = Action(
            kind, command if kind == "terminal" else "", path, self.name, self.kind, dangerous, assessment.reason,
        )
        decision = evaluate(self.rules, action, shorthand=self.shorthand, attended=attended)
        if decision.verdict == "allow":
            if decision.source == "shorthand":
                label = "auto:off" if decision.rule == "approvals:off" else "auto:smart"
            else:
                label = "auto:rule" if decision.source == "rule" else "auto:default"
            return Ruling(True, "", label, decision, decision.layer, decision.rule)
        if decision.verdict == "deny":
            return Ruling(False, self._denial(decision, assessment), "denied:unattended" if decision.source == "unattended" else "denied:rule",
                          decision, decision.layer, decision.rule)
        risk = "dangerous" if dangerous else "manual"
        # a subagent task's run is answered in its parent's chat, attributed to the task
        target_agent, target_chat = run.approval_target or (self.agent_id, run.chat_id)
        signature = action_signature(action)
        grants = getattr(run.broker, "session_allows", None)
        if not dangerous and grants is not None and grants(self.user_id, target_agent, target_chat, signature):
            return Ruling(True, "", "approved:session", decision, decision.layer, decision.rule, scope="session")
        # a rule's reason says which rule asked; the policy's reason says why the action is dangerous
        if decision.source == "shorthand":
            reason = assessment.reason or decision.reason
        else:
            reason = decision.reason + (f"; {assessment.reason}" if dangerous and assessment.reason else "")
        options, learn = self._offer(run, action, decision, signature)
        outcome = run.broker.request(
            self.user_id, target_agent, target_chat, run, tool, display + (f"\n{detail}" if detail else ""), cwd,
            reason, risk, backend=self.name, task=run.task, **({"options": options, "learn": learn} if learn else {}),
        )
        approval_id = getattr(outcome, "approval_id", "") or ""
        if outcome.reason == "cancelled" and run.cancellation is not None:
            run.cancellation.raise_if_cancelled()  # the user interrupted: end the turn, not just this tool
        if outcome.approved:
            return Ruling(True, "", "approved", decision, decision.layer, decision.rule, approval_id,
                          getattr(outcome, "scope", "") or "once")
        return Ruling(
            False, f"Denied: the user did not approve this action ({outcome.reason}). {DENIED_HINT}",
            f"denied:{outcome.reason}", decision, decision.layer, decision.rule, approval_id, "deny",
        )

    def _offer(self, run, action: Action, decision: Decision, signature: str) -> tuple[tuple[str, ...], Learn | None]:
        """The approve scopes the card offers and what they remember. Only a plain chat run offers more than `once`
        (a subagent task asks in its parent's chat and is never remembered); `agent` needs the service's writer."""
        if run.task is not None or getattr(run, "approval_target", None) is not None:
            return ("once",), None
        scopes = offered_scopes(self.rules, action, shorthand=self.shorthand, decision=decision)
        writer = getattr(run, "learn_rules", None)
        if "agent" in scopes and writer is None:
            scopes = tuple(scope for scope in scopes if scope != "agent")
        if len(scopes) == 1:
            return scopes, None
        rules = rules_for_action(action) if "agent" in scopes else []
        agent_id = self.agent_id
        persist = (lambda learned: writer(agent_id, learned)) if "agent" in scopes else None
        return scopes, Learn(signature, rules, persist, self.add_rules)

    @staticmethod
    def _denial(decision: Decision, assessment: Assessment) -> str:
        """The text for the model when a rule denies, or when an `ask` became a denial because nobody can answer."""
        if decision.source != "unattended":
            return f"Denied: {decision.reason}. {DENIED_HINT}"
        mode = (decision.rule or "").removeprefix("approvals:") if (decision.rule or "").startswith("approvals:") else ""
        if mode == "manual":
            return f"Denied: approvals are manual and nobody is attached to approve actions here. {DENIED_HINT}"
        if mode == "off":
            return f"Denied: {decision.reason} and nobody is attached to approve it, whatever the approval mode. {DENIED_HINT}"
        if mode == "smart":
            return f"Denied: this action is dangerous ({decision.reason}) and nobody is attached to approve it. {DENIED_HINT}"
        return f"Denied: this action needs approval ({decision.reason}) and nobody is attached to approve it. {DENIED_HINT}"

    def approval_note(self, decision: str, ruling: Ruling | None = None) -> str:
        """One short line for the model: how a command that ran was authorized (so it knows whether the user saw it)."""
        if decision == "approved:session":
            return "[approved by the user earlier in this session]"
        if decision.startswith("approved"):
            return "[approved by the user]"
        if decision == "auto:off":
            return "[approvals off]"
        if decision == "auto:smart":
            run = current_run()
            if self.unattended or run is None or not run.interactive:
                return "[ran without approval: smart mode, nobody attached to approve]"
            return "[ran without approval: smart mode, normal command]"
        if decision in ("auto:rule", "auto:default"):
            by = ruling.rule if ruling is not None and ruling.rule else (ruling.layer + " default" if ruling and ruling.layer else "approval rules")
            return f"[ran without approval: allowed by {by}]"
        return ""

    # ------------------------------------------------------------------ execution
    def authorize(
        self, tool: str, display: str, command: str, *, cwd: str | None = None, assessment: Assessment | None = None,
        preapproved: bool = False, detail: str = "", path: str = "",
    ) -> tuple[tuple[str, Ruling, Assessment, str] | None, str | None]:
        """The checks before anything runs: validation, the fixed floor, the approval rules and the audit file.

        Returns ((cwd, ruling, assessment, decision), None) when the action may run, else (None, text for the model).
        Refusals are audited here; the caller audits the run itself.
        """
        cwd = cwd or self.workdir
        if "\x00" in command or "\x00" in display:
            self._audit(tool, display.replace("\x00", "\\0"), cwd, "denied:invalid", None, 0.0)
            return None, "error: the action contains a NUL byte"
        if len(command) > MAX_COMMAND_CHARS:
            self._audit(tool, display, cwd, "denied:invalid", None, 0.0)
            return None, f"error: command is longer than {MAX_COMMAND_CHARS} characters; use sandbox_write for large content"
        verdict = assessment or assess(
            command, None, self.protected(), self.local_cwd(cwd) if self.local else None,
        )
        try:
            if preapproved and verdict.level != "protected":  # the second step of an operation whose first step was already authorized
                ruling = Ruling(True, "", "approved:same-operation")
            else:
                ruling = self.decide(tool, display, verdict, cwd, detail, command=command, path=path)
        except GenerationCancelled:
            self._audit(tool, display, cwd, "cancelled", None, 0.0, verdict=verdict.level)
            raise
        decision = ruling.label
        if not ruling.allowed:
            self._audit(tool, display, cwd, decision, None, 0.0, verdict=verdict.level, ruling=ruling)
            logger.info("terminal %s/%s not run (%s): %s", self.agent_id, self.name, decision, display[:200])
            return None, ruling.text
        if not audit_writable(self.audit_file):  # fail closed: nothing runs that cannot be recorded
            logger.error("terminal %s/%s not run: the audit file is not writable", self.agent_id, self.name)
            return None, AUDIT_FAILED
        return (cwd, ruling, verdict, decision), None

    def run(
        self, tool: str, display: str, command: str, *, cwd: str | None = None, timeout: int | float | None = None,
        stdin: bytes | str | None = None, assessment: Assessment | None = None, preapproved: bool = False,
        detail: str = "", path: str = "",
    ) -> tuple[Result | None, str | None]:
        """Authorize and execute; returns (result, None) or (None, denial text for the model).

        `detail` is extra text for the approval card only (a preview of what a file tool will write); it is not audited.
        `path` is the resolved target of a file tool, for the `path` match of the approval rules.
        """
        allowed, denial = self.authorize(
            tool, display, command, cwd=cwd, assessment=assessment, preapproved=preapproved, detail=detail, path=path,
        )
        if denial is not None:
            return None, denial
        cwd, ruling, verdict, decision = allowed
        started = time.monotonic()
        result = self.backend.run(command, cwd, self.timeout_for(timeout), stdin)
        elapsed = time.monotonic() - started
        result.approval = self.approval_note(decision, ruling)
        self._audit(tool, display, cwd, decision, result.exit_code, elapsed, timed_out=result.timed_out, verdict=verdict.level,
                    ruling=ruling)
        logger.info(
            "terminal %s/%s [%s] %s exit=%s decision=%s %.1fs: %s",
            self.user_id, self.agent_id, self.name, tool, result.exit_code, decision, elapsed, display[:200],
        )
        return result, None

    def _audit(self, tool: str, command: str, cwd: str, decision: str, exit_code: int | None, elapsed: float,
               *, timed_out: bool = False, verdict: str | None = None, ruling: Ruling | None = None) -> bool:
        extra: dict = {"verdict": verdict} if verdict else {}
        if ruling is not None:  # which layer and rule decided, what they said, and the approval card that was answered
            if ruling.layer:
                extra["layer"] = ruling.layer
            if ruling.rule:
                extra["rule"] = ruling.rule
            if ruling.decision is not None:
                extra["rule_verdict"] = ruling.decision.verdict
            elif ruling.layer == "floor":
                extra["rule_verdict"] = "deny"
            if ruling.approval_id:
                extra["approval_id"] = ruling.approval_id
            if ruling.scope:
                extra["scope"] = ruling.scope
        return write_audit(
            self.audit_file, user=self.user_id, agent=self.agent_id, backend=self.name,
            tool=tool, command=command, cwd=cwd, decision=decision, exit_code=exit_code,
            duration_ms=int(elapsed * 1000), timed_out=timed_out, **extra,
        )

    def audit_refusal(self, tool: str, display: str, reason: str) -> None:
        """Record an action refused before it reached `run` (an invalid path, for instance)."""
        self._audit(tool, display, self.workdir, "denied:invalid", None, 0.0)
        logger.info("terminal %s/%s refused (%s): %s", self.agent_id, self.name, reason, display[:200])
