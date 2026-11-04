"""Setup status of the Claude Code CLI for one user: installed, version, logged in (via the CLI itself)."""
from __future__ import annotations

import json
import os
import subprocess

from src.config.models import ClaudeSubscriptionConfig
from src.providers.claude_subscription import native
from src.providers.claude_subscription.catalog import parse_version

LOGIN_HINT = "Run `claude auth login` on the computer running RECALL, as the account RECALL runs as."
# the only fields of `claude auth status` passed on; the account e-mail and organization never leave here
_AUTH_FIELDS = {"authMethod": "auth_method", "subscriptionType": "subscription"}


def _run(command: list[str], env: dict[str, str], timeout: float) -> subprocess.CompletedProcess:
    return subprocess.run(
        command, env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, encoding="utf-8",
        errors="replace", timeout=timeout, cwd=native.workdir(),
    )


def cli_status(settings: ClaudeSubscriptionConfig, *, timeout: float = 20.0) -> dict:
    """What the provider would find: the CLI, its version and whether it has a usable login.

    Login state comes from `claude auth status`; RECALL never reads Claude credential files, and this
    never logs in. `logged_in` is None when the CLI's answer could not be read.
    """
    status: dict = {"cli_found": False, "command": None, "version": None, "logged_in": None, "hint": None}
    try:
        env = native.child_env(dict(os.environ), config_dir=settings.claude_config_dir, telemetry=settings.claude_code_telemetry)
    except native.ClaudeSubscriptionError as exc:
        return status | {"hint": str(exc)}
    command = native.resolve_command([settings.command] if settings.command else None, env)
    if command is None:
        return status | {"hint": native.INSTALL_HINT}
    status |= {"cli_found": True, "command": command[0]}
    try:
        version = parse_version(_run([*command, "--version"], env, timeout).stdout)
        status["version"] = ".".join(map(str, version)) if version else None
        answer = _run([*command, "auth", "status"], env, timeout)
    except (OSError, subprocess.SubprocessError) as exc:
        return status | {"hint": f"Claude Code could not be started: {type(exc).__name__}"}
    try:
        report = json.loads(answer.stdout)
    except ValueError:
        report = None
    if not isinstance(report, dict) or not isinstance(report.get("loggedIn"), bool):
        return status | {"hint": "Claude Code did not report its login state; run `claude auth status` to check."}
    status["logged_in"] = report["loggedIn"]
    status |= {name: report.get(key) for key, name in _AUTH_FIELDS.items() if isinstance(report.get(key), str)}
    if not report["loggedIn"]:
        status["hint"] = LOGIN_HINT + (
            f" Set CLAUDE_CONFIG_DIR={settings.claude_config_dir} for that command." if settings.claude_config_dir else ""
        )
    return status
