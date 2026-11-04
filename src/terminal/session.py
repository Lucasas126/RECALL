"""One agent's terminal: the backend, the approval policy and the audit trail around every action."""

from __future__ import annotations

import time
from pathlib import Path

from src.config.models import AgentTerminalConfig, TerminalSettings
from src.providers.cancellation import GenerationCancelled
from src.terminal.audit import write_audit
from src.terminal.backends import Backend, Result
from src.terminal.context import current_run
from src.terminal.policy import Assessment, assess
from src.utils.logger import get_logger

logger = get_logger("terminal")
MAX_COMMAND_CHARS = 20_000

DENIED_HINT = "Do not retry this action; tell the user what you wanted to do and why, and let them decide."


class TerminalSession:
    """Runs commands through a backend after the approval policy allowed them.

    `unattended` forces the no-client behaviour (dream sessions); otherwise the active chat run decides: no
    run context, or one flagged non-interactive, is unattended too.
    """

    def __init__(
        self, backend: Backend, config: AgentTerminalConfig, settings: TerminalSettings, *,
        user_id: str, agent_id: str, audit_file: Path | None = None, unattended: bool = False,
    ):
        self.backend = backend
        self.config = config
        self.settings = settings
        self.user_id = user_id
        self.agent_id = agent_id
        self.audit_file = audit_file
        self.unattended = unattended

    # ------------------------------------------------------------------ limits
    @property
    def output_limit(self) -> int:
        return min(self.config.max_output_chars, self.settings.max_output_chars)

    def timeout_for(self, requested: int | float | None) -> float:
        default = min(self.config.timeout_seconds, self.settings.max_timeout_seconds)
        if requested is None:
            return float(default)
        try:
            value = float(requested)
        except (TypeError, ValueError):
            return float(default)
        return float(max(1.0, min(value, self.settings.max_timeout_seconds)))

    # ------------------------------------------------------------------ policy
    def decide(self, tool: str, display: str, assessment: Assessment, cwd: str) -> tuple[bool, str, str]:
        """(allowed, text for the model when denied, decision label for the audit trail)."""
        mode = self.config.approvals
        if assessment.level == "hardline":
            why = assessment.reason or "matches the hardline blocklist"
            return False, (
                f"Blocked: this action ({why}) is on the hardline blocklist and can never run in this sandbox, "
                f"whatever the approval mode. {DENIED_HINT}"
            ), "hardline"
        if mode == "off":
            return True, "", "auto:off"
        run = current_run()
        dangerous = assessment.level == "dangerous"
        attended = not self.unattended and run is not None and run.interactive
        if not attended:
            if mode == "manual":
                return False, f"Denied: approvals are manual and nobody is attached to approve actions here. {DENIED_HINT}", "denied:unattended"
            if dangerous:
                return False, f"Denied: this action is dangerous ({assessment.reason}) and nobody is attached to approve it. {DENIED_HINT}", "denied:unattended"
            return True, "", "auto:smart"
        if mode == "smart" and not dangerous:
            return True, "", "auto:smart"
        risk = "dangerous" if dangerous else "manual"
        outcome = run.broker.request(
            self.user_id, self.agent_id, run.chat_id, run, tool, display, cwd,
            assessment.reason or ("approvals are set to manual" if mode == "manual" else ""), risk,
        )
        if outcome.reason == "cancelled" and run.cancellation is not None:
            run.cancellation.raise_if_cancelled()  # the user interrupted: end the turn, not just this tool
        if outcome.approved:
            return True, "", "approved"
        return False, f"Denied: the user did not approve this action ({outcome.reason}). {DENIED_HINT}", f"denied:{outcome.reason}"

    # ------------------------------------------------------------------ execution
    def run(
        self, tool: str, display: str, command: str, *, cwd: str | None = None, timeout: int | float | None = None,
        stdin: bytes | str | None = None, assessment: Assessment | None = None, preapproved: bool = False,
    ) -> tuple[Result | None, str | None]:
        """Authorize and execute; returns (result, None) or (None, denial text for the model)."""
        cwd = cwd or self.config.workdir
        if "\x00" in command or "\x00" in display:
            return None, "error: the action contains a NUL byte"
        if len(command) > MAX_COMMAND_CHARS:
            return None, f"error: command is longer than {MAX_COMMAND_CHARS} characters; use sandbox_write for large content"
        verdict = assessment or assess(command, None)
        try:
            if preapproved:  # the second step of an operation whose first step was already authorized
                allowed, text, decision = True, "", "approved:same-operation"
            else:
                allowed, text, decision = self.decide(tool, display, verdict, cwd)
        except GenerationCancelled:
            self._audit(tool, display, cwd, "cancelled", None, 0.0)
            raise
        if not allowed:
            self._audit(tool, display, cwd, decision, None, 0.0)
            logger.info("terminal %s not run (%s): %s", self.agent_id, decision, display[:200])
            return None, text
        started = time.monotonic()
        result = self.backend.run(command, cwd, self.timeout_for(timeout), stdin)
        elapsed = time.monotonic() - started
        self._audit(tool, display, cwd, decision, result.exit_code, elapsed, timed_out=result.timed_out)
        logger.info(
            "terminal %s/%s %s exit=%s decision=%s %.1fs: %s",
            self.user_id, self.agent_id, tool, result.exit_code, decision, elapsed, display[:200],
        )
        return result, None

    def _audit(self, tool: str, command: str, cwd: str, decision: str, exit_code: int | None, elapsed: float,
               *, timed_out: bool = False) -> None:
        write_audit(
            self.audit_file, user=self.user_id, agent=self.agent_id, backend=getattr(self.backend, "name", ""),
            tool=tool, command=command, cwd=cwd, decision=decision, exit_code=exit_code,
            duration_ms=int(elapsed * 1000), timed_out=timed_out,
        )
