"""Resolve an agent's `terminal` block against its user's `terminal_backends`."""

from __future__ import annotations

from src.config.models import AgentTerminalConfig, TerminalBackendConfig, UserConfig
from src.utils.i18n import InputError


def resolve_backend(
    user: UserConfig, terminal: AgentTerminalConfig | None, *, agent_id: str, allow_local: bool,
) -> TerminalBackendConfig:
    """The backend config an agent's `terminal` block names; raises InputError with a clear reason otherwise."""
    if terminal is None:
        raise InputError(
            "agent '{agent_id}' uses the terminal tool group but has no terminal.backend", agent_id=agent_id,
        )
    backend = user.terminal_backends.get(terminal.backend)
    if backend is None:
        raise InputError(
            "terminal backend '{backend}' is not defined in this user's terminal_backends", backend=terminal.backend,
        )
    if backend.type == "local" and not allow_local:
        raise InputError(
            "terminal backend '{backend}' is of type local, which is disabled (settings.yaml terminal.allow_local)",
            backend=terminal.backend,
        )
    return backend


def check_agent_terminal(user: UserConfig, tool_groups: list[str] | None, terminal: AgentTerminalConfig | None,
                         *, agent_id: str, allow_local: bool) -> None:
    """Service-level validation: an agent that lists the `terminal` group needs a usable backend."""
    if "terminal" in (tool_groups or ()):
        resolve_backend(user, terminal, agent_id=agent_id, allow_local=allow_local)
