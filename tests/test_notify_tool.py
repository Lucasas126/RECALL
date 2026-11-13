from __future__ import annotations

import unittest
from unittest.mock import Mock

from src.agent.toolsets import notify
from src.agent.toolsets.notify import MAX_PER_HOUR, build_notify_tools
from src.config import ceiling
from src.config.levels import Level


def _agent(user: str = "user-a", agent: str = "bot") -> Mock:
    instance = Mock()
    instance.config.user_id, instance.config.id = user, agent
    return instance


def _notifier(linked: bool = True) -> Mock:
    notifier = Mock()
    notifier.linked.return_value = linked
    return notifier


class NotifyToolTests(unittest.TestCase):
    def setUp(self) -> None:
        notify._sent.clear()
        self.addCleanup(notify._sent.clear)

    def tool(self, notifier, agent=None):
        return build_notify_tools(agent or _agent(), lambda: notifier)[0]

    def test_sends_only_to_the_owner_and_has_no_recipient_parameter(self) -> None:
        notifier = _notifier()
        send = self.tool(notifier)
        self.assertEqual(set(send.tool_spec["function"]["parameters"]["properties"]), {"text"})
        self.assertIn("sent", send("Done: **report** ready"))
        notifier.send_markdown.assert_called_once_with("user-a", "Done: **report** ready")

    def test_errors_the_agent_can_read(self) -> None:
        self.assertIn("no bot token", self.tool(None)("hi"))
        self.assertIn("no Telegram account", self.tool(_notifier(linked=False))("hi"))
        self.assertIn("empty", self.tool(_notifier())("   "))
        self.assertIn("characters", self.tool(_notifier())("x" * 4000))
        failing = _notifier()
        failing.send_markdown.side_effect = RuntimeError("HTTP 500")
        self.assertIn("did not accept", self.tool(failing)("hi"))

    def test_hourly_limit_is_per_agent_and_a_failed_send_does_not_count(self) -> None:
        notifier = _notifier()
        send = self.tool(notifier)
        for _ in range(MAX_PER_HOUR):
            self.assertIn("sent", send("hi"))
        self.assertIn("limit of 30", send("hi"))
        self.assertEqual(notifier.send_markdown.call_count, MAX_PER_HOUR)
        self.assertIn("sent", self.tool(notifier, _agent(agent="other"))("hi"))  # another agent has its own count
        failing = _notifier()
        failing.send_markdown.side_effect = RuntimeError("x")
        other = self.tool(failing, _agent(agent="third"))
        for _ in range(MAX_PER_HOUR + 2):
            self.assertIn("did not accept", other("hi"))

    def test_the_window_slides(self) -> None:
        for _ in range(MAX_PER_HOUR):
            self.assertIsNone(notify._take_slot("u", "a", now=0.0))
        self.assertIsNotNone(notify._take_slot("u", "a", now=10.0))
        self.assertIsNone(notify._take_slot("u", "a", now=3601.0))

    def test_level_and_disposable_chats(self) -> None:
        self.assertEqual(ceiling.GROUP_MIN_LEVEL["notify"], Level.ASSISTANT)
        self.assertFalse(ceiling.tool_allowed(Level.READER, "notify_telegram"))
        self.assertTrue(ceiling.tool_allowed(Level.ASSISTANT, "notify_telegram"))
        from src.agent.tool_groups import DISPOSABLE_BLOCKED_TOOLS
        self.assertIn("notify_telegram", DISPOSABLE_BLOCKED_TOOLS)


if __name__ == "__main__":
    unittest.main()
