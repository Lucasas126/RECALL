"""The `automations` tool group: schedule things the agent does later, for the user who owns it.

`automation_create` stores a record and returns at once; when it is due the service starts an unattended task for the
agent (see `src/service/automations.py`). The owner and the agent are never parameters: they come from the run, so an
agent can only schedule and manage its OWN automations, for its OWN user. The tools reach the service through the
`RunContext` the chat service binds around each reply.
"""

from __future__ import annotations

import json
from typing import Annotated, Callable, Optional

from src.agent.tooling import tool
from src.config.models import ResolvedAgent
from src.service.automations import AutomationError, describe
from src.terminal.context import current_run

def _summary(record: dict) -> dict:
    return {
        "id": record["id"], "title": record["title"], "when": describe(record["trigger"]),
        "enabled": record["enabled"], "status": record["status"], "next_run": record.get("next_run"),
        "last_run": record.get("last_run"), "runs": record["runs"],
        "last_outcome": (record["history"][-1]["outcome"] if record["history"] else None),
    }


def _trigger(at: str, every_minutes: int, cron: str, after_process: str) -> dict:
    given = [name for name, value in (("at", at), ("every_minutes", every_minutes), ("cron", cron), ("after_process", after_process)) if value]
    if len(given) != 1:
        raise AutomationError(f"say when with exactly one of at, every_minutes, cron, after_process (got {', '.join(given) or 'none'})")
    if at:
        return {"kind": "at", "at": at}
    if every_minutes:
        return {"kind": "every", "minutes": every_minutes}
    if cron:
        return {"kind": "cron", "cron": cron}
    return {"kind": "process_end", "process": after_process}


def build_automation_tools(config: ResolvedAgent) -> list[Callable[..., str]]:
    """The automation tools of an agent."""

    def port() -> tuple[str, object] | str:
        run = current_run()
        service = getattr(run, "automations", None) if run is not None else None
        if run is None or service is None or run.task is not None or run.user_id != config.user_id or run.agent_id != config.id:
            return "error: automations are not available in this context"
        return run.user_id, service

    def mine(service, user_id: str, automation_id: str) -> dict:
        record = service.store.get(user_id, automation_id) if automation_id else None
        if record is None or record["agent_id"] != config.id:
            raise AutomationError(f"unknown automation: {automation_id!r} (use automation_list to see yours)")
        return record

    @tool
    def automation_create(
        instruction: Annotated[str, "what you must do when it fires, written for yourself as a self-contained brief (you will not have this chat); at most 4000 characters"],
        title: Annotated[str, "short name shown in lists; optional"] = "",
        at: Annotated[str, "run once at this local date and time, like 2026-11-20T09:00"] = "",
        every_minutes: Annotated[int, "run every this many minutes (5 or more)"] = 0,
        cron: Annotated[str, "run on this five-field cron schedule, like '30 8 * * mon-fri'"] = "",
        after_process: Annotated[str, "run once when this background process ends, like p1a2b3c4d"] = "",
    ) -> str:
        """Schedule something to do later without the user in the chat: a reminder, a daily summary, a follow-up when a
        background process ends. Set exactly one of at, every_minutes, cron or after_process. When it fires you run unattended
        with your own SOUL: nobody can approve anything, so what would ask is denied. You may use notify_telegram and send_file
        then to tell the user. At most 50 automations per user; none runs more than 6 times an hour."""
        found = port()
        if isinstance(found, str):
            return found
        user_id, service = found
        try:
            record = service.create(
                user_id, config.id, instruction, _trigger(at, every_minutes, cron, after_process), title=title, created_by=config.id,
            )
        except AutomationError as exc:
            return f"error: {exc}"
        return "scheduled: " + json.dumps(_summary(record), ensure_ascii=False)

    @tool
    def automation_list() -> str:
        """List your automations (id, title, when, state, next run, last outcome)."""
        found = port()
        if isinstance(found, str):
            return found
        user_id, service = found
        records = service.list(user_id, config.id)
        return json.dumps([_summary(r) for r in records], ensure_ascii=False) if records else "no automations"

    @tool
    def automation_edit(
        automation_id: Annotated[str, "the id from automation_list"],
        instruction: Annotated[str, "the new brief; empty keeps the current one"] = "",
        title: Annotated[str, "the new title; empty keeps the current one"] = "",
        at: Annotated[str, "a new one-off time, like 2026-11-20T09:00"] = "",
        every_minutes: Annotated[int, "a new interval in minutes (5 or more)"] = 0,
        cron: Annotated[str, "a new cron schedule"] = "",
        after_process: Annotated[str, "a new process to wait for"] = "",
    ) -> str:
        """Change one of your automations. Give a new `when` (one of at, every_minutes, cron, after_process) only to change it."""
        found = port()
        if isinstance(found, str):
            return found
        user_id, service = found
        try:
            mine(service, user_id, automation_id)
            new_when = _trigger(at, every_minutes, cron, after_process) if any((at, every_minutes, cron, after_process)) else None
            record = service.edit(
                user_id, automation_id, instruction=instruction or None, title=title or None, trigger=new_when,
            )
        except AutomationError as exc:
            return f"error: {exc}"
        return "updated: " + json.dumps(_summary(record), ensure_ascii=False)

    def switch(automation_id: str, enabled: bool) -> str:
        found = port()
        if isinstance(found, str):
            return found
        user_id, service = found
        try:
            mine(service, user_id, automation_id)
            record = service.set_enabled(user_id, automation_id, enabled)
        except AutomationError as exc:
            return f"error: {exc}"
        return ("resumed: " if enabled else "paused: ") + json.dumps(_summary(record), ensure_ascii=False)

    @tool
    def automation_pause(automation_id: Annotated[str, "the id from automation_list"]) -> str:
        """Pause an automation: it stops firing until you resume it."""
        return switch(automation_id, False)

    @tool
    def automation_resume(automation_id: Annotated[str, "the id from automation_list"]) -> str:
        """Resume a paused automation; what it missed while paused is not replayed."""
        return switch(automation_id, True)

    @tool
    def automation_delete(automation_id: Annotated[str, "the id from automation_list"]) -> str:
        """Delete one of your automations for good."""
        found = port()
        if isinstance(found, str):
            return found
        user_id, service = found
        try:
            mine(service, user_id, automation_id)
            service.delete(user_id, automation_id)
        except AutomationError as exc:
            return f"error: {exc}"
        return f"deleted {automation_id}"

    return [automation_create, automation_list, automation_edit, automation_pause, automation_resume, automation_delete]
