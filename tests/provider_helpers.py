from __future__ import annotations

import os
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


def isolate_providers(
    testcase, *, openrouter_key: str | None = "test-key", variable: str = "OPENROUTER_API_KEY",
    claude: dict | None = None, ollama: list[str] | None = None,
) -> dict:
    """Make provider availability deterministic for one test, never touching the network or a real `claude`.

    OpenRouter is available when `openrouter_key` is set (None unsets the variable); Claude Code follows the
    `claude` status dict (default: not installed); Ollama serves the `ollama` model names (default: unreachable).
    Returns the started mocks (`claude`, `ollama`) so a test can change them.
    """
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
