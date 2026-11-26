from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.service.chat import ChatEvent, UnknownChat
from src.telegram.bot import TelegramBotState, TelegramSession, _send_generation


class PrunedChatTests(unittest.IsolatedAsyncioTestCase):
    """An empty chat can be pruned before its first message; the reply then goes to a fresh chat of the same agent."""

    def setUp(self) -> None:
        self.service = Mock()
        self.service.agent_owner.side_effect = lambda person, agent: person
        self.service.get_preferences.return_value = {"show_thinking": False, "language": "en"}
        self.service.list_agents.return_value = [{"id": "agent-a", "name": "First"}]
        self.service.create_chat.return_value = {"id": "fresh-chat"}
        self.session = TelegramSession("user-a", "agent-a", "pruned-chat")
        self.state = TelegramBotState(self.service, {123: self.session})
        message = SimpleNamespace(reply_text=AsyncMock())
        self.update = SimpleNamespace(
            effective_user=SimpleNamespace(id=123), effective_chat=SimpleNamespace(id=456, type="private"),
            effective_message=message,
        )
        self.context = SimpleNamespace(bot=Mock(), application=SimpleNamespace(bot_data={"state": self.state}))
        stream = Mock(start=AsyncMock(), finish=AsyncMock(), update=AsyncMock(), attach=Mock())
        patcher = patch("src.telegram.bot._ReplyStream", return_value=stream)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.stream = stream

    async def test_an_unknown_chat_is_replaced_and_the_message_sent_again(self) -> None:
        reply = iter([ChatEvent("text", text="hi")])
        self.service.send.side_effect = [UnknownChat("unknown chat: {chat_id}", chat_id="pruned-chat"), reply]
        await _send_generation(self.update, self.context, "hello", [])
        self.service.create_chat.assert_called_once_with("user-a", "agent-a", origin="telegram")
        self.assertEqual([call.args[2] for call in self.service.send.call_args_list], ["pruned-chat", "fresh-chat"])
        self.assertEqual(self.session.chat_id, "fresh-chat")
        self.service.update_preferences.assert_not_called()
        self.assertEqual(self.state.busy, {})
        shown = self.stream.finish.await_args.args[0]
        self.assertFalse(any("failed" in text for text, _ in shown))

    async def test_a_second_unknown_chat_is_reported_not_looped(self) -> None:
        self.service.send.side_effect = UnknownChat("unknown chat: {chat_id}", chat_id="x")
        await _send_generation(self.update, self.context, "hello", [])
        self.assertEqual(self.service.send.call_count, 2)
        self.assertEqual(self.state.busy, {})


if __name__ == "__main__":
    unittest.main()
