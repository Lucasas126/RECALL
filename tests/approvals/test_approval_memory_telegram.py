"""Approval memory on Telegram: the buttons the card offers, their callback data and what each tap asks of the service."""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

os.environ.setdefault("RECALL_SESSION_SECRET", "x")

from src.telegram.bot import (
    TelegramBotState, TelegramSession, _approval_buttons, _approval_outcome, _send_approval, approval_callback,
    _close_approval,
)
from src.terminal.approvals import ApprovalForbidden
from src.utils.i18n import messages

DATA = {"id": "0123456789ab", "tool": "terminal", "command": "git status", "cwd": "~/workspace", "reason": "needs approval",
        "risk": "manual", "backend": "box", "timeout_seconds": 300, "expires_at": 1.0, "options": ["once", "session", "agent"]}


def callbacks(markup) -> list[str]:
    return [button.callback_data for row in markup.inline_keyboard for button in row]


class ApprovalButtonTests(unittest.TestCase):
    def test_a_full_card_has_four_buttons_and_every_callback_fits_telegram(self) -> None:
        markup = _approval_buttons("en", DATA["id"], DATA["options"])
        data = callbacks(markup)
        self.assertEqual([d.rsplit(":", 1)[1] for d in data], ["y", "n", "s", "a"])
        for item in data:
            self.assertLessEqual(len(item.encode()), 64)
            self.assertTrue(item.startswith(f"ap:{DATA['id']}:"))
        labels = [button.text for row in markup.inline_keyboard for button in row]
        self.assertEqual(labels, ["✅ Approve once", "❌ Deny", "✅ Always in this session", "✅ Always for this agent"])

    def test_a_card_without_options_keeps_the_two_original_buttons(self) -> None:
        for options in (None, ["once"], "garbage"):
            with self.subTest(options=options):
                markup = _approval_buttons("en", DATA["id"], options)
                self.assertEqual([d.rsplit(":", 1)[1] for d in callbacks(markup)], ["y", "n"])
                self.assertEqual(markup.inline_keyboard[0][0].text, "✅ Approve")

    def test_only_the_offered_scopes_get_buttons(self) -> None:
        markup = _approval_buttons("en", DATA["id"], ["once", "session"])
        self.assertEqual([d.rsplit(":", 1)[1] for d in callbacks(markup)], ["y", "n", "s"])

    def test_the_longest_possible_id_still_fits(self) -> None:
        self.assertLessEqual(max(len(d.encode()) for d in callbacks(_approval_buttons("en", "f" * 12, DATA["options"]))), 64)

    def test_spanish_labels_exist(self) -> None:
        markup = _approval_buttons("es", DATA["id"], DATA["options"])
        self.assertEqual([b.text for row in markup.inline_keyboard for b in row][2:], ["✅ Siempre en esta sesión", "✅ Siempre para este agente"])
        catalog = messages("es")
        for text in ("✅ Approved for this session", "✅ Approved always for this agent"):
            self.assertIn(text, catalog)

    def test_the_outcome_line_names_the_scope(self) -> None:
        self.assertEqual(_approval_outcome("en", "approved", "", "session"), "✅ Approved for this session")
        self.assertEqual(_approval_outcome("en", "approved", "", "agent"), "✅ Approved always for this agent")
        self.assertEqual(_approval_outcome("en", "approved", "", "once"), "✅ Approved")
        self.assertEqual(_approval_outcome("en", "denied", "", "deny"), "❌ Denied")


class ApprovalCallbackTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.service = Mock()
        self.session = TelegramSession("test-user", "agent-a", "chat-1")
        self.state = TelegramBotState(self.service, {123: self.session})
        self.bot = Mock()
        self.bot.send_message = AsyncMock(return_value=SimpleNamespace(message_id=77))
        self.bot.edit_message_text = AsyncMock()
        self.context = SimpleNamespace(bot=self.bot, application=SimpleNamespace(bot_data={"state": self.state}), user_data={}, args=[])

    def query(self, data: str, user_id: int = 123):
        query = SimpleNamespace(data=data, message=SimpleNamespace(message_id=77), answer=AsyncMock(), edit_message_reply_markup=AsyncMock())
        update = SimpleNamespace(effective_user=SimpleNamespace(id=user_id), effective_chat=SimpleNamespace(id=456, type="private"),
                                 effective_message=query.message, callback_query=query)
        return query, update

    async def tap(self, choice: str, result: dict, user_id: int = 123):
        self.service.resolve_approval.reset_mock()
        self.service.resolve_approval.return_value = result
        await _send_approval(self.context, self.state, self.session, 456, DATA, "en")
        query, update = self.query(f"ap:{DATA['id']}:{choice}", user_id)
        await approval_callback(update, self.context)
        return query

    async def test_each_button_asks_the_service_for_its_scope(self) -> None:
        for choice, decision, scope in (("y", "approve", "once"), ("s", "approve", "session"), ("a", "approve", "agent"), ("n", "deny", "once")):
            with self.subTest(choice=choice):
                query = await self.tap(choice, {"status": "denied" if decision == "deny" else "approved", "scope": scope})
                self.service.resolve_approval.assert_called_once_with("test-user", "agent-a", "chat-1", DATA["id"], decision, scope)
                query.answer.assert_awaited_once_with()  # no alert: it worked
                self.assertNotIn(DATA["id"], self.state.approvals)

    async def test_the_card_shows_what_was_granted(self) -> None:
        await self.tap("a", {"status": "approved", "scope": "agent"})
        self.assertIn("Approved always for this agent", self.bot.edit_message_text.await_args.args[0])
        await self.tap("s", {"status": "approved", "scope": "session"})
        self.assertIn("Approved for this session", self.bot.edit_message_text.await_args.args[0])

    async def test_a_failed_always_is_reported_and_the_card_shows_a_plain_approval(self) -> None:
        query = await self.tap("a", {"status": "approved", "scope": "once", "error": "a blanket allow is not allowed"})
        self.assertTrue(query.answer.await_args.kwargs["show_alert"])
        self.assertIn("blanket allow", query.answer.await_args.args[0])
        text = self.bot.edit_message_text.await_args.args[0]
        self.assertIn("Approved", text)
        self.assertNotIn("always", text)

    async def test_an_unknown_choice_letter_does_nothing(self) -> None:
        query = await self.tap("x", {"status": "approved"})
        self.service.resolve_approval.assert_not_called()
        self.assertTrue(query.answer.await_args.kwargs["show_alert"])

    async def test_a_foreign_telegram_user_cannot_answer_and_the_owner_check_stays(self) -> None:
        self.state.sessions[999] = TelegramSession("someone-else", "agent-a", "chat-1")
        await _send_approval(self.context, self.state, self.session, 456, DATA, "en")
        query, update = self.query(f"ap:{DATA['id']}:a", user_id=999)
        await approval_callback(update, self.context)
        self.service.resolve_approval.assert_not_called()  # the card belongs to test-user
        self.assertTrue(query.answer.await_args.kwargs["show_alert"])
        self.service.resolve_approval.side_effect = ApprovalForbidden("this approval belongs to another user")
        query, update = self.query(f"ap:{DATA['id']}:a")
        await approval_callback(update, self.context)
        self.assertTrue(query.answer.await_args.kwargs["show_alert"])
        self.assertIn(DATA["id"], self.state.approvals)  # still open

    async def test_the_resolved_event_scope_reaches_the_closing_line(self) -> None:
        await _send_approval(self.context, self.state, self.session, 456, DATA, "en")
        await _close_approval(self.bot, self.state, DATA["id"], "approved", "approved", "en", "session")
        self.assertIn("Approved for this session", self.bot.edit_message_text.await_args.args[0])

    async def test_the_sent_message_has_the_offered_buttons(self) -> None:
        await _send_approval(self.context, self.state, self.session, 456, DATA, "en")
        self.assertEqual(len(callbacks(self.bot.send_message.await_args.kwargs["reply_markup"])), 4)
        await _send_approval(self.context, self.state, self.session, 456, {**DATA, "options": ["once"], "id": "ffffffffffff"}, "en")
        self.assertEqual(len(callbacks(self.bot.send_message.await_args.kwargs["reply_markup"])), 2)


if __name__ == "__main__":
    unittest.main()
