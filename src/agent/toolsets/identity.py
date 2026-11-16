"""The `identity` tool group: the agent changes its own display name.

Only the caller's own `name` changes (never its id, level, tool groups, rules or SOUL), so no approval card is asked. The
checks, the write and the audit line live in `AgentAdmin.set_display_name` (`src/service/agent_admin.py`). Not built in a
disposable chat or an automation's task: it writes the agent's persistent configuration. A chat in progress keeps the
prompt it started with; the new name shows from the next chat.
"""

from __future__ import annotations

from typing import Annotated, Callable

from src.agent.tooling import tool
from src.config.models import ResolvedAgent
from src.service.agent_permissions import PermissionRefused
from src.terminal.context import current_run

UNAVAILABLE = "error: the display name can only be changed in a chat with the user (not in dreams, scheduled tasks or workers)"


def build_identity_tools(config: ResolvedAgent) -> list[Callable[..., str]]:
    """The `set_display_name` tool of an agent; it can only ever act on `config.id`."""

    @tool
    def set_display_name(
        name: Annotated[str, "the new display name: one line, not empty, at most 100 characters"],
    ) -> str:
        """Change YOUR OWN display name (the name the user sees), for example when the user gives you one. It changes
        nothing else. The new name shows in new chats; this conversation keeps its current prompt."""
        run = current_run()
        port = getattr(run, "agents", None) if run is not None else None
        if run is None or port is None or run.task is not None or run.user_id != config.user_id or run.agent_id != config.id:
            return UNAVAILABLE
        try:
            result = port.set_display_name(config.user_id, config.id, config.level, name, chat_id=run.chat_id)
        except PermissionRefused as exc:
            return f"error: {exc}"
        if result["status"] == "unchanged":
            return f"unchanged: your display name is already '{result['name']}'"
        return f"done: your display name is now '{result['name']}' (it was '{result['before']}'); new chats use it"

    return [set_display_name]
