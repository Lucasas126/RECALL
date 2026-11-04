"""Who may use the Claude Code CLI's default login (the OS account running RECALL).

A user with their own `claude_config_dir` always uses it. A user without one would silently spend the login of
whoever runs the server, so that is allowed only for the user ids listed in settings.yaml
`claude_subscription.default_login_users` (empty list = nobody). Fails closed: an unknown user is not allowed.
"""
from __future__ import annotations

from src.config import settings
from src.config.models import ClaudeSubscriptionConfig
from src.utils.i18n import InputError

NO_OWN_LOGIN_CODE = "claude_no_own_login"
NO_OWN_LOGIN_MESSAGE = (
    "Claude subscription models are not available to you: you have no Claude login folder of your own, and the "
    "server's default Claude login is reserved for its owner. Ask the administrator to set up your own Claude login "
    "folder, or choose another model."
)


def login_allowed(user_id: str | None, claude: ClaudeSubscriptionConfig) -> bool:
    """True when `user_id` has its own login folder or is listed to use the default login."""
    if claude.claude_config_dir:
        return True
    return bool(user_id) and user_id in settings.CLAUDE_LOGIN.default_login_users


def require_login(user_id: str | None, claude: ClaudeSubscriptionConfig) -> None:
    """Raise the coded error unless `user_id` may use the login `claude` selects (call before any CLI process)."""
    if not login_allowed(user_id, claude):
        raise no_own_login()


def no_own_login() -> InputError:
    return InputError(NO_OWN_LOGIN_MESSAGE, code=NO_OWN_LOGIN_CODE)
