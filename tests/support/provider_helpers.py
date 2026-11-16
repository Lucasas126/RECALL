from __future__ import annotations

import os
from types import SimpleNamespace
from unittest.mock import patch

from src.providers.ollama import OllamaUnavailable

CLAUDE_NOT_FOUND = {"cli_found": False, "command": None, "version": None, "logged_in": None, "hint": "not installed"}
CLAUDE_LOGGED_IN = {
    "cli_found": True, "command": "claude", "version": "2.1.316", "logged_in": True, "hint": None,
    "auth_method": "claude.ai", "subscription": "max",
}
CLAUDE_LOGGED_OUT = {
    "cli_found": True, "command": "claude", "version": "2.1.316", "logged_in": False,
    "hint": "Run `claude auth login` on the computer running RECALL, as the account RECALL runs as.",
}


class _Everyone(list):
    """A default-login user list that contains every user id (the fixtures' stand-in for a permissive owner list)."""

    def __contains__(self, item) -> bool:
        return True


def set_default_login_users(testcase, users) -> None:
    """Make `users` (a list of ids) the users allowed to use Claude Code's default login for one test."""
    login = patch("src.config.settings.CLAUDE_LOGIN", SimpleNamespace(default_login_users=users, command=None))
    login.start()
    testcase.addCleanup(login.stop)


def allow_every_default_login(testcase) -> None:
    set_default_login_users(testcase, _Everyone())


def isolate_providers(
    testcase, *, openrouter_key: str | None = "test-key", variable: str = "OPENROUTER_API_KEY",
    claude: dict | None = None, ollama: list[str] | None = None, default_login_users=None,
) -> dict:
    """Make provider availability deterministic for one test, never touching the network or a real `claude`.

    OpenRouter is available when `openrouter_key` is set (None unsets the variable); Claude Code follows the
    `claude` status dict (default: not installed); Ollama serves the `ollama` model names (default: unreachable).
    Every user may use the default Claude login unless `default_login_users` (a list of ids) says otherwise.
    Returns the started mocks (`claude`, `ollama`) so a test can change them.
    """
    set_default_login_users(testcase, _Everyone() if default_login_users is None else default_login_users)
    environment = patch.dict(os.environ)
    environment.start()
    testcase.addCleanup(environment.stop)
    if openrouter_key is None:
        os.environ.pop(variable, None)
    else:
        os.environ[variable] = openrouter_key
    status = patch("src.service.providers.cli_status", return_value=claude or CLAUDE_NOT_FOUND)
    if ollama is not None:
        models = patch("src.service.providers.list_ollama_models", return_value=ollama)
    else:
        models = patch("src.service.providers.list_ollama_models", side_effect=OllamaUnavailable("Ollama is down"))
    started = {"claude": status.start(), "ollama": models.start()}
    testcase.addCleanup(models.stop)
    testcase.addCleanup(status.stop)
    return started
