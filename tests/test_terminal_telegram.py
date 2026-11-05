from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.service.chat import ChatEvent
from src.telegram.bot import (
    TelegramBotState, TelegramSession, _approval_text, _close_approval, _send_approval, approval_callback,
)
from src.terminal.approvals import ApprovalForbidden, ApprovalNotFound

DATA = {"id": "0123456789ab", "tool": "terminal", "command": "rm -rf build", "cwd": "~/workspace",
        "reason": "recursive delete", "risk": "dangerous", "backend": "pc", "timeout_seconds": 300, "expires_at": 1.0}


class TelegramApprovalTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.service = Mock()
        self.session = TelegramSession("test-user", "agent-a", "chat-1")
        self.state = TelegramBotState(self.service, {123: self.session})
        self.bot = Mock()
        self.bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=77))
        self.bot.edit_message_text = AsyncMock()
        self.context = SimpleNamespace(bot=self.bot, application=SimpleNamespace(bot_data={"state": self.state}),
                                       user_data={}, args=[])

    def query(self, data: str, message_id: int = 77, user_id: int = 123):
        query = SimpleNamespace(data=data, message=SimpleNamespace(message_id=message_id), answer=AsyncMock(),
                                edit_message_reply_markup=AsyncMock())
        update = SimpleNamespace(effective_user=SimpleNamespace(id=user_id), effective_chat=SimpleNamespace(id=456, type="private"),
                                 effective_message=query.message, callback_query=query)
        return query, update

    async def send(self, data=DATA):
        await _send_approval(self.context, self.state, self.session, 456, data, "en")

    async def test_message_has_the_command_the_reason_and_two_small_buttons(self) -> None:
        await self.send()
        args, kwargs = self.bot.send_message.await_args
        self.assertEqual(args[0], 456)
        self.assertIn("rm -rf build", args[1])
        self.assertIn("recursive delete", args[1])
        self.assertIn("300", args[1])
        buttons = [b for row in kwargs["reply_markup"].inline_keyboard for b in row]
        self.assertEqual([b.callback_data.rsplit(":", 1)[1] for b in buttons], ["y", "n"])
        for button in buttons:
            self.assertLess(len(button.callback_data.encode()), 64)  # Telegram's limit
            self.assertIn(DATA["id"], button.callback_data)
        self.assertIn(DATA["id"], self.state.approvals)

    async def test_message_shows_the_backend(self) -> None:
        self.assertIn("Backend: pc", _approval_text("en", DATA))
        self.assertNotIn("Backend:", _approval_text("en", {k: v for k, v in DATA.items() if k != "backend"}))

    async def test_long_commands_are_cut_and_fences_cannot_break_out(self) -> None:
        text = _approval_text("en", {**DATA, "command": "echo " + "x" * 5000 + " ``` injected"})
        self.assertLess(len(text), 2000)
        self.assertNotIn("``` injected", text)

    async def test_approve_and_deny_taps_resolve_through_the_service(self) -> None:
        for choice, decision, status in (("y", "approve", "approved"), ("n", "deny", "denied")):
            with self.subTest(choice=choice):
                self.service.resolve_approval.reset_mock()
                self.service.resolve_approval.return_value = {"status": status}
                await self.send()
                query, update = self.query(f"ap:{DATA['id']}:{choice}")
                await approval_callback(update, self.context)
                self.service.resolve_approval.assert_called_once_with(
                    "test-user", "agent-a", "chat-1", DATA["id"], decision)
                query.answer.assert_awaited_once()
                self.assertNotIn(DATA["id"], self.state.approvals)
                edited = self.bot.edit_message_text.await_args
                self.assertIs(edited.kwargs["reply_markup"], None)  # buttons dropped
                self.assertIn("Approved" if status == "approved" else "Denied", edited.args[0])

    async def test_an_expired_or_foreign_card_is_refused_without_calling_the_service(self) -> None:
        query, update = self.query("ap:unknown:y")
        await approval_callback(update, self.context)
        self.assertTrue(query.answer.await_args.kwargs["show_alert"])
        await self.send()
        query, update = self.query(f"ap:{DATA['id']}:y", message_id=999)  # a different message
        await approval_callback(update, self.context)
        query, update = self.query(f"ap:{DATA['id']}:x")  # not y/n
        await approval_callback(update, self.context)
        self.service.resolve_approval.assert_not_called()

    async def test_a_tap_after_the_service_expired_it_closes_the_card(self) -> None:
        self.service.resolve_approval.side_effect = ApprovalNotFound("unknown or expired approval: {approval_id}", approval_id="x")
        await self.send()
        query, update = self.query(f"ap:{DATA['id']}:y")
        await approval_callback(update, self.context)
        self.assertTrue(query.answer.await_args.kwargs["show_alert"])
        self.assertNotIn(DATA["id"], self.state.approvals)
        self.assertIn("timed out", self.bot.edit_message_text.await_args.args[0])

    async def test_a_forbidden_answer_keeps_the_card_open(self) -> None:
        self.service.resolve_approval.side_effect = ApprovalForbidden("this approval belongs to another user")
        await self.send()
        query, update = self.query(f"ap:{DATA['id']}:y")
        await approval_callback(update, self.context)
        self.assertTrue(query.answer.await_args.kwargs["show_alert"])

    async def test_an_unknown_telegram_user_cannot_answer(self) -> None:
        await self.send()
        query, update = self.query(f"ap:{DATA['id']}:y", user_id=999)
        with patch("src.telegram.bot._unauthorized", new=AsyncMock()) as unauthorized:
            await approval_callback(update, self.context)
        unauthorized.assert_awaited_once()
        self.service.resolve_approval.assert_not_called()

    async def test_expiry_edits_the_message_once(self) -> None:
        await self.send()
        for _ in range(2):
            await _close_approval(self.bot, self.state, DATA["id"], "denied", "timed out", "en")
        self.assertEqual(self.bot.edit_message_text.await_count, 1)
        self.assertIn("timed out", self.bot.edit_message_text.await_args.args[0])
        self.assertNotIn(DATA["id"], self.state.approvals)


if __name__ == "__main__":
    unittest.main()
