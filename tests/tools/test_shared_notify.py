"""Notifications of an agent owned by a space reach every member that has Telegram; private agents are unchanged."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from src.agent.toolsets import notify
from src.agent.toolsets.files_send import build_files_send_tools
from src.agent.toolsets.notify import build_notify_tools
from src.config import spaces

SPACE = "space-0a1b2c3d"


def _write_config(root: Path) -> None:
    for user in ("user-a", "user-b", "user-c"):
        (root / user).mkdir(parents=True)
        (root / user / "user.yaml").write_text(
            f"id: {user}\nname: {user}\nmax_level: 3\nprovider: {{name: openrouter, api_key: KEY_NAME}}\nagents: []\npassword_hash: x\n",
            encoding="utf-8",
        )
    (root / SPACE).mkdir()
    (root / SPACE / "user.yaml").write_text(
        f"id: {SPACE}\nname: Shared\nkind: space\nmembers: [user-a, user-b, user-c]\nbilling_user: user-a\n"
        "provider: {name: openrouter, api_key: KEY_NAME}\nagents: [nova]\n",
        encoding="utf-8",
    )


def _notifier(linked: set[str]) -> Mock:
    notifier = Mock()
    notifier.linked.side_effect = lambda person: person in linked
    return notifier


def _agent(owner: str) -> Mock:
    agent = Mock()
    agent.config.user_id, agent.config.id = owner, "nova"
    agent.config.name = "Nova"
    agent._turn_files, agent.outbox = [], []
    return agent


class SharedNotifyTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        _write_config(self.root)
        spaces.clear_spaces_cache()
        self.addCleanup(spaces.clear_spaces_cache)
        notify._sent.clear()
        self.addCleanup(notify._sent.clear)
        for target in ("src.agent.toolsets.notify.CONFIG_DIR", "src.agent.toolsets.files_send.CONFIG_DIR", "src.service.dream.CONFIG_DIR"):
            patcher = patch(target, self.root)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_targets(self) -> None:
        self.assertEqual(spaces.notification_targets(SPACE, self.root), ["user-a", "user-b", "user-c"])
        self.assertEqual(spaces.notification_targets("user-a", self.root), ["user-a"])

    def test_notify_goes_to_every_linked_member_and_skips_the_rest(self) -> None:
        notifier = _notifier({"user-a", "user-b"})
        result = build_notify_tools(_agent(SPACE), lambda: notifier)[0]("hello")
        self.assertIn("sent", result)
        self.assertEqual([call.args[0] for call in notifier.send_markdown.call_args_list], ["user-a", "user-b"])

    def test_one_failing_member_does_not_stop_the_others(self) -> None:
        notifier = _notifier({"user-a", "user-b"})
        notifier.send_markdown.side_effect = [RuntimeError("HTTP 500"), 1]
        result = build_notify_tools(_agent(SPACE), lambda: notifier)[0]("hello")
        self.assertIn("1 of 2", result)
        self.assertEqual(notifier.send_markdown.call_count, 2)

    def test_nobody_linked_is_an_error(self) -> None:
        result = build_notify_tools(_agent(SPACE), lambda: _notifier(set()))[0]("hello")
        self.assertIn("no Telegram account", result)

    def test_private_agent_is_unchanged(self) -> None:
        notifier = _notifier({"user-a", "user-b"})
        build_notify_tools(_agent("user-a"), lambda: notifier)[0]("hello")
        notifier.send_markdown.assert_called_once_with("user-a", "hello")

    def test_send_file_goes_to_every_linked_member(self) -> None:
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        notifier = _notifier({"user-b", "user-c"})
        agent = _agent(SPACE)
        send = build_files_send_tools(agent, lambda: notifier)[0]
        with patch("src.agent.toolsets.files_send.fetch_file", return_value=("a.txt", b"hi")):
            result = send("notes:a.txt", "", True)
        self.assertIn("sent a.txt", result)
        self.assertEqual([call.args[0] for call in notifier.send_file.call_args_list], ["user-b", "user-c"])

    def test_dream_notification_reaches_every_member(self) -> None:
        from src.service.dream import DreamManager
        sent = Mock()
        manager = DreamManager.__new__(DreamManager)
        manager._deps = Mock(notifier=sent, language=lambda person: "en", public_url=None)
        config = Mock(user_id=SPACE, id="nova")
        config.name = "Nova"
        manager._notify(config, {"id": "s1", "status": "completed", "started_at": "2026-11-03T04:01:00+01:00"})
        self.assertEqual([call.args[0] for call in sent.call_args_list], ["user-a", "user-b", "user-c"])


if __name__ == "__main__":
    unittest.main()
