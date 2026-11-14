"""The `secrets` tool group: manage the names in `.config/secrets.env` without ever seeing a value.

Design: the local terminal refuses `secrets.env` to every agent (src/terminal/protected.py) so a prompt injection cannot
read the keys. This group keeps that: no tool returns, accepts or logs a value. An admin agent may list the NAMES (and
whether each has a value), copy one variable to another, rename one and delete one. Each change shows the user a card,
offers only *Approve once* and *Deny*, and is audited with names only. A dream, a scheduled task or a worker has no one to
ask: the tools are not built there, and they refuse again if called. Putting a NEW value stays the user's job
(`python -m src.admin set-secret NAME`, or a per-user key in Settings).
"""
from __future__ import annotations

import json
from typing import Annotated, Callable

from src.agent.toolsets.permissions import APPROVAL_OPTIONS, DENIED_HINT
from src.agent.tooling import tool
from src.config import secrets
from src.config.settings import DATA_DIR
from src.config.ceiling import secrets_allowed
from src.config.models import ResolvedAgent
from src.terminal.audit import audit_path, audit_writable, write_permission_audit
from src.terminal.context import RunContext, current_run
from src.service.agent_permissions import clean_text

UNAVAILABLE = (
    "error: the secrets tools are only available in a chat with the user, who must approve every change "
    "(not in dreams, scheduled tasks or workers)"
)
REASON = "Changing a stored secret always needs your approval, once. It cannot be set to always allow."


def secrets_notice(config: ResolvedAgent) -> str:
    return (
        "## Managing secrets\n\n"
        "With the secrets tools you can list the NAMES of the stored secrets (never their values) and, when the user asks, "
        "copy one variable to another, rename one or delete one. Each change shows the user a card and waits for Approve or "
        "Deny, and every attempt is recorded. You never see, receive or write a value: a new value is set by the user with "
        "`python -m src.admin set-secret NAME` or in Settings. Never ask for a key in the chat, never retry a denial."
    )


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def build_secrets_tools(config: ResolvedAgent) -> list[Callable[..., str]]:
    """The secrets tools of an agent; empty below the level admin."""
    if config.level is None or not secrets_allowed(config.level):
        return []

    def available() -> RunContext | str:
        run = current_run()
        if (
            run is None or getattr(run, "broker", None) is None or not run.interactive or run.task is not None
            or run.approval_target is not None or run.user_id != config.user_id or run.agent_id != config.id
        ):
            return UNAVAILABLE
        return run

    def audit(run: RunContext, tool_name: str, action: str, before: object, after: object, decision: str, **extra) -> None:
        write_permission_audit(
            audit_path(DATA_DIR, run.user_id, config.id), actor=config.id, user=run.user_id, agent=config.id,
            target="secrets.env", tool=tool_name, field="secret", action=action, before=before, after=after,
            decision=decision, **{k: v for k, v in extra.items() if v},
        )

    def change(tool_name: str, action: str, lines: list[str], before: object, after: object, apply: Callable[[], None]) -> str:
        ready = available()
        if isinstance(ready, str):
            return ready
        run = ready
        if not audit_writable(audit_path(DATA_DIR, run.user_id, config.id)):  # fail closed: no unrecorded change of a secret
            return "error: the audit file is not writable, so nothing can be changed. Tell the user."
        card = "\n".join([
            f"Agent '{clean_text(config.name)}' [{config.id}] (level {config.level.label}) asks to {action}.",
            *lines,
            "No value is shown to the agent or written to the audit log. Approving applies exactly this change, once.",
        ])
        outcome = run.broker.request(
            run.user_id, run.agent_id, run.chat_id, run, tool_name, card, "", REASON, "manual", options=APPROVAL_OPTIONS,
        )
        approval_id = getattr(outcome, "approval_id", "") or ""
        if outcome.reason == "cancelled" and run.cancellation is not None:
            run.cancellation.raise_if_cancelled()
        if not outcome.approved or getattr(outcome, "scope", "once") not in ("", "once"):
            audit(run, tool_name, action, before, after, "denied", reason=outcome.reason, approval_id=approval_id, chat=run.chat_id)
            return f"error: the user did not approve this change ({outcome.reason}). {DENIED_HINT}"
        try:
            apply()
        except (KeyError, FileExistsError, ValueError, OSError) as exc:
            audit(run, tool_name, action, before, after, "approved", reason=type(exc).__name__, approval_id=approval_id,
                  chat=run.chat_id, applied=False)
            return f"error: {_explain(exc)}. Nothing was changed."
        audit(run, tool_name, action, before, after, "approved", approval_id=approval_id, chat=run.chat_id, applied=True)
        return _json({"status": "applied", "approved_by": "user", "change": after})

    def _explain(exc: Exception) -> str:
        if isinstance(exc, KeyError):
            return f"{exc.args[0]} has no value in secrets.env"
        if isinstance(exc, FileExistsError):
            return f"{exc.args[0]} already exists (delete it first if you want to replace it)"
        if isinstance(exc, ValueError):
            return str(exc)
        return "the secrets file could not be written"

    def check(*names: str) -> str | None:
        for name in names:
            try:
                secrets.check_name(name)
            except ValueError as exc:
                return f"error: {exc}"
        return None

    @tool
    def secret_list() -> str:
        """List the NAMES stored in secrets.env and whether each has a value. Never returns a value. Read-only."""
        ready = available()
        if isinstance(ready, str):
            return ready
        return _json({"secrets": [{"name": name, "has_value": bool(value.strip())} for name, value in sorted(secrets.read_secrets().items())]})

    @tool
    def secret_copy(
        source: Annotated[str, "name of the variable whose value is copied (must have a value in secrets.env)"],
        target: Annotated[str, "name of the variable that receives it (created, or overwritten)"],
    ) -> str:
        """Copy the value of one stored secret to another name, without you ever seeing it. Needs the user's approval."""
        if (problem := check(source, target)):
            return problem
        if source == target:
            return "error: source and target are the same variable"
        exists = target in secrets.read_secrets()
        lines = [f"Change: copy the value of {source} to {target}" + (" (OVERWRITES the current value of the target)." if exists else " (new variable).")]
        return change("secret_copy", "COPY a secret", lines, {"target_existed": exists}, {"copied": source, "to": target},
                      lambda: secrets.copy_secret(source, target))

    @tool
    def secret_rename(
        old: Annotated[str, "current variable name"],
        new: Annotated[str, "new name; it must not exist yet"],
    ) -> str:
        """Rename a stored secret (the value moves, you never see it). Variables in user.yaml that name the old one keep naming it. Needs approval."""
        if (problem := check(old, new)):
            return problem
        lines = [f"Change: rename {old} to {new}. Any user.yaml or settings that still name {old} will find it empty."]
        return change("secret_rename", "RENAME a secret", lines, {"name": old}, {"name": new}, lambda: secrets.rename_secret(old, new))

    @tool
    def secret_delete(
        name: Annotated[str, "variable name to remove from secrets.env"],
    ) -> str:
        """Delete one stored secret. The value is lost: only the user can set it again. Needs approval."""
        if (problem := check(name)):
            return problem
        if name not in secrets.read_secrets():
            return _json({"status": "unchanged", "reason": f"{name} is not in secrets.env"})
        lines = [f"Change: DELETE {name} from secrets.env. The value cannot be recovered from here."]
        return change("secret_delete", "DELETE a secret", lines, {"name": name, "set": True}, {"name": name, "set": False},
                      lambda: secrets.delete_secret(name))

    return [secret_list, secret_copy, secret_rename, secret_delete]
