"""Claude subscription hardening: bare command names in user.yaml and login folders confined to the user."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).parent))

from src.config import settings
from src.config.models import ClaudeLoginSettings, ClaudeSubscriptionConfig
from src.providers.claude_subscription import claude_subscription_provider
from src.providers.claude_subscription.login import command_for, login_allowed, own_login_dir
from src.utils.i18n import InputError


def _login(users=(), command=None):
    return patch("src.config.settings.CLAUDE_LOGIN", SimpleNamespace(default_login_users=list(users), command=command))


class CommandTests(unittest.TestCase):
    def test_user_yaml_command_must_be_a_bare_name(self) -> None:
        self.assertEqual(ClaudeSubscriptionConfig(command="claude").command, "claude")
        for bad in ("/usr/bin/claude", "./claude", "bin/claude", "C:\\x\\claude.exe", "..\\claude"):
            with self.subTest(bad=bad), self.assertRaisesRegex(ValidationError, "bare executable name"):
                ClaudeSubscriptionConfig(command=bad)

    def test_admin_command_must_be_absolute_and_is_used_when_the_user_sets_none(self) -> None:
        self.assertEqual(ClaudeLoginSettings(command="/opt/claude/bin/claude").command, "/opt/claude/bin/claude")
        with self.assertRaisesRegex(ValidationError, "absolute"):
            ClaudeLoginSettings(command="claude")
        with _login(command="/opt/claude"):
            self.assertEqual(command_for(ClaudeSubscriptionConfig()), ["/opt/claude"])
            self.assertEqual(command_for(ClaudeSubscriptionConfig(command="claude2")), ["claude2"])
        with _login():
            self.assertIsNone(command_for(ClaudeSubscriptionConfig()))


class LoginFolderTests(unittest.TestCase):
    def setUp(self) -> None:
        self.base = (settings.CONFIG_DIR / "user-a").resolve()

    def test_folder_inside_the_users_own_config_dir_is_accepted(self) -> None:
        for value in ("claude-login", "sub/login", (self.base / "claude-login").as_posix()):
            with self.subTest(value=value), _login():
                claude = ClaudeSubscriptionConfig(claude_config_dir=value)
                self.assertTrue(login_allowed("user-a", claude))
                self.assertTrue(own_login_dir("user-a", claude).startswith(self.base.as_posix() + "/"))

    def test_folder_outside_is_refused_even_for_default_login_users(self) -> None:
        other = (settings.CONFIG_DIR / "user-b" / "claude-login").resolve().as_posix()
        for value in ("../user-b/claude-login", other, "/home/admin/.claude", "..", "."):
            with self.subTest(value=value), _login(users=["user-a"]):
                claude = ClaudeSubscriptionConfig(claude_config_dir=value)
                self.assertFalse(login_allowed("user-a", claude))
                with self.assertRaises(InputError):
                    claude_subscription_provider(claude, user_id="user-a")

    def test_default_login_still_needs_the_listing(self) -> None:
        with _login(users=["admin"]):
            self.assertTrue(login_allowed("admin", ClaudeSubscriptionConfig()))
            self.assertFalse(login_allowed("user-a", ClaudeSubscriptionConfig()))

    def test_provider_receives_the_resolved_folder(self) -> None:
        with _login():
            provider = claude_subscription_provider(ClaudeSubscriptionConfig(claude_config_dir="claude-login"), user_id="user-a")
        self.assertEqual(provider.config_dir, (self.base / "claude-login").as_posix())

    def test_symlink_pointing_outside_is_refused(self) -> None:
        import tempfile
        with tempfile.TemporaryDirectory() as tmp, patch.object(settings, "CONFIG_DIR", Path(tmp)):
            (Path(tmp) / "user-a").mkdir()
            (Path(tmp) / "user-a" / "link").symlink_to(Path(tmp))
            with _login():
                self.assertFalse(login_allowed("user-a", ClaudeSubscriptionConfig(claude_config_dir="link")))


if __name__ == "__main__":
    unittest.main()
