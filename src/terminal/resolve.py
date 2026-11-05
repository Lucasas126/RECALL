"""Resolve an agent's `terminal` block against its user's `terminal_backends`."""

from __future__ import annotations

from src.config.models import AgentTerminalConfig, TerminalBackendConfig, UserConfig
from src.utils.i18n import InputError
from src.utils.logger import get_logger

logger = get_logger("terminal")


def resolve_backend(
    user: UserConfig, name: str, *, allow_local: bool,
) -> TerminalBackendConfig:
    """The user's backend `name`; raises InputError with a clear reason when it is unknown or disabled."""
    backend = user.terminal_backends.get(name)
    if backend is None:
        raise InputError(
            "terminal backend '{backend}' is not defined in this user's terminal_backends", backend=name,
        )
    if backend.type == "local" and not allow_local:
        raise InputError(
            "terminal backend '{backend}' is of type local, which is disabled (settings.yaml terminal.allow_local)",
            backend=name,
        )
    return backend


def resolve_backends(
    user: UserConfig, terminal: AgentTerminalConfig | None, *, agent_id: str, allow_local: bool,
) -> dict[str, TerminalBackendConfig]:
    """The allowed backends that resolve, by name. One that fails is dropped with a warning; none left raises."""
    if terminal is None:
        raise InputError(
            "agent '{agent_id}' uses the terminal tool group but has no terminal.backends", agent_id=agent_id,
        )
    resolved: dict[str, TerminalBackendConfig] = {}
    first_error: InputError | None = None
    for name in terminal.backends:
        try:
            resolved[name] = resolve_backend(user, name, allow_local=allow_local)
        except InputError as exc:
            first_error = first_error or exc
            logger.warning("agent %s/%s: terminal backend '%s' disabled: %s", user.id, agent_id, name, exc)
    if not resolved:
        assert first_error is not None
        raise first_error
    return resolved


def check_agent_terminal(user: UserConfig, tool_groups: list[str] | None, terminal: AgentTerminalConfig | None,
                         *, agent_id: str, allow_local: bool) -> None:
    """Service-level validation: an agent that lists the `terminal` group needs at least one usable backend."""
    if "terminal" in (tool_groups or ()):
        resolve_backends(user, terminal, agent_id=agent_id, allow_local=allow_local)
