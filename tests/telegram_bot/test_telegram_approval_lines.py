from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from telegram.error import BadRequest

from src.telegram.bot import (
    TelegramBotState, TelegramSession, _ReplyContent, _approval_line, _close_approval, _generation_message, _send_approval,
)

DATA = {"id": "0123456789ab", "tool": "terminal", "command": "rm -rf build\nls", "cwd": "~", "reason": "x",
        "risk": "dangerous", "backend": "pc", "timeout_seconds": 300, "expires_at": 1.0}


class ApprovalLineTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.session = TelegramSession("u", "a", "c")
        self.state = TelegramBotState(Mock(), {1: self.session})
        self.bot = Mock()
        self.bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=77))
        self.bot.edit_message_text = AsyncMock()
        self.bot.delete_message = AsyncMock()
        self.context = SimpleNamespace(bot=self.bot)
        self.stream = SimpleNamespace(ended=False, content=_ReplyContent(), show_thinking=True, update=AsyncMock())

    def test_the_line_is_the_tool_and_the_first_line_of_the_command(self) -> None:
        self.assertEqual(_approval_line(DATA), '>terminal("rm -rf build")')
        long = _approval_line({**DATA, "command": "echo " + "x" * 500})
        self.assertLess(len(long), 160)
        self.assertTrue(long.endswith('…")'))

    async def test_an_answered_card_vanishes_and_becomes_a_line_with_a_tick(self) -> None:
        await _send_approval(self.context, self.state, self.session, 456, DATA, "en", self.stream)
        await _close_approval(self.bot, self.state, DATA["id"], "approved", "approved", "en")
        self.bot.delete_message.assert_awaited_once_with(chat_id=456, message_id=77)
        self.bot.edit_message_text.assert_not_awaited()
        self.assertEqual(self.stream.content.parts, [("reasoning_tool", '✅ >terminal("rm -rf build")')])
        self.stream.update.assert_awaited_once()
        self.assertTrue(self.stream.update.await_args.kwargs["force"])

    async def test_a_denied_card_gets_a_cross(self) -> None:
        await _send_approval(self.context, self.state, self.session, 456, DATA, "en", self.stream)
        await _close_approval(self.bot, self.state, DATA["id"], "denied", "denied by user", "en")
        self.assertEqual(self.stream.content.parts[0][1][0], "❌")

    async def test_the_line_lives_inside_the_collapsible_block(self) -> None:
        await _send_approval(self.context, self.state, self.session, 456, DATA, "en", self.stream)
        await _close_approval(self.bot, self.state, DATA["id"], "approved", "approved", "en")
        _, entities = _generation_message(self.stream.content.parts)
        self.assertEqual([e.type for e in entities if e.type == "expandable_blockquote"], ["expandable_blockquote"])

    async def test_without_a_live_reply_the_card_stays_with_its_outcome(self) -> None:
        for stream in (None, SimpleNamespace(ended=True, content=_ReplyContent(), show_thinking=True, update=AsyncMock())):
            with self.subTest(stream=stream):
                self.bot.edit_message_text.reset_mock()
                self.bot.delete_message.reset_mock()
                await _send_approval(self.context, self.state, self.session, 456, DATA, "en", stream)
                await _close_approval(self.bot, self.state, DATA["id"], "denied", "timed out", "en")
                self.bot.delete_message.assert_not_awaited()
                self.assertEqual(self.bot.edit_message_text.await_count, 1)

    async def test_when_the_message_cannot_be_deleted_the_card_keeps_its_outcome(self) -> None:
        self.bot.delete_message = AsyncMock(side_effect=BadRequest("Message can't be deleted"))
        await _send_approval(self.context, self.state, self.session, 456, DATA, "en", self.stream)
        await _close_approval(self.bot, self.state, DATA["id"], "approved", "approved", "en")
        self.assertEqual(self.bot.edit_message_text.await_count, 1)

    async def test_closing_twice_adds_one_line(self) -> None:
        await _send_approval(self.context, self.state, self.session, 456, DATA, "en", self.stream)
        for _ in range(2):
            await _close_approval(self.bot, self.state, DATA["id"], "approved", "approved", "en")
        self.assertEqual(len(self.stream.content.parts), 1)


if __name__ == "__main__":
    unittest.main()
