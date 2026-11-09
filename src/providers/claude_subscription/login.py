"""Who may use the Claude Code CLI's default login (the OS account running RECALL).

A user with their own `claude_config_dir` always uses it. A user without one would silently spend the login of
whoever runs the server, so that is allowed only for the user ids listed in settings.yaml
`claude_subscription.default_login_users` (empty list = nobody). Fails closed: an unknown user is not allowed.
"""
from __future__ import annotations

import re
from pathlib import Path

from src.config import settings
from src.config.models import ClaudeSubscriptionConfig
from src.utils.i18n import InputError

_SAFE_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
NO_OWN_LOGIN_CODE = "claude_no_own_login"
NO_OWN_LOGIN_MESSAGE = (
    "Claude subscription models are not available to you: you have no Claude login folder of your own, and the "
    "server's default Claude login is reserved for its owner. Ask the administrator to set up your own Claude login "
    "folder, or choose another model."
)


def own_login_dir(user_id: str | None, claude: ClaudeSubscriptionConfig) -> str | None:
    """The user's login folder resolved, or None when none is configured.

    The folder must lie inside the user's own config folder (`<CONFIG_DIR>/<user_id>/`; a relative path is
    relative to it), so a user can never point at another user's login or at the server's default one.
    Raises the coded `claude_no_own_login` error when it resolves anywhere else.
    """
    if not claude.claude_config_dir:
        return None
    if not user_id or not _SAFE_ID.match(user_id):
        raise no_own_login()
    base = (settings.CONFIG_DIR / user_id).resolve()
    target = (base / Path(claude.claude_config_dir)).resolve()  # an absolute path replaces `base` in the join
    if target == base or not target.is_relative_to(base):
        raise no_own_login()
    return target.as_posix()


def login_allowed(user_id: str | None, claude: ClaudeSubscriptionConfig) -> bool:
    """True when `user_id` has a login folder inside its own config folder or is listed for the default login."""
    if claude.claude_config_dir:
        try:
            return own_login_dir(user_id, claude) is not None
        except InputError:
            return False
    return bool(user_id) and user_id in settings.CLAUDE_LOGIN.default_login_users


def resolved_settings(user_id: str | None, claude: ClaudeSubscriptionConfig) -> ClaudeSubscriptionConfig:
    """`claude` with its login folder resolved and checked (raises like `require_login`)."""
    require_login(user_id, claude)
    folder = own_login_dir(user_id, claude)
    return claude.model_copy(update={"claude_config_dir": folder}) if folder else claude


def command_for(claude: ClaudeSubscriptionConfig) -> list[str] | None:
    """The executable to run: the user's bare name, else the administrator's absolute path, else the default lookup."""
    command = claude.command or settings.CLAUDE_LOGIN.command
    return [command] if command else None


def require_login(user_id: str | None, claude: ClaudeSubscriptionConfig) -> None:
    """Raise the coded error unless `user_id` may use the login `claude` selects (call before any CLI process)."""
    if not login_allowed(user_id, claude):
        raise no_own_login()


def no_own_login() -> InputError:
    return InputError(NO_OWN_LOGIN_MESSAGE, code=NO_OWN_LOGIN_CODE)
