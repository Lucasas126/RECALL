from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.config.preferences import UserPreferences
from src.service.chat import ChatEvent
from src.telegram.bot import (
    TelegramBotState, TelegramSession, _offer_reroute, _send_generation, new_chat, panel_callback,
    reroute_callback, text_message,
)


class TelegramRerouteTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.preferences = {"show_thinking": True, "language": "en", "agent_id": "agent-a"}
        self.service = Mock()
        self.service.get_preferences.side_effect = lambda user: self.preferences.copy()
        self.service.list_agents.return_value = [
            {"id": "agent-a", "name": "First"}, {"id": "agent-b", "name": "Second"},
        ]
        self.service.create_chat.side_effect = lambda user, agent: {"id": f"chat-of-{agent}"}
        self.service.chat_summary.return_value = {"id": "old-chat", "title": "Old topic", "turns": 4}
        self.service.update_preferences.side_effect = (
            lambda user, changes: self.preferences.update(changes.model_dump(exclude_unset=True))
        )
        self.session = TelegramSession("test-user", "agent-a", "old-chat")
        self.state = TelegramBotState(self.service, {123: self.session})
        self.message = SimpleNamespace(text="hello there", reply_text=AsyncMock(return_value=SimpleNamespace(message_id=55)))
        self.update = SimpleNamespace(
            effective_user=SimpleNamespace(id=123), effective_chat=SimpleNamespace(id=456, type="private"),
            effective_message=self.message,
        )
        self.context = SimpleNamespace(
            bot=Mock(), application=SimpleNamespace(bot_data={"state": self.state}), user_data={}, args=[],
        )
        self.context.bot.delete_message = AsyncMock()
        self.context.bot.edit_message_reply_markup = AsyncMock()

    def markup_buttons(self) -> list:
        markup = self.message.reply_text.await_args.kwargs["reply_markup"]
        return [button for row in markup.inline_keyboard for button in row]

    def query(self, data: str, message_id: int):
        query = SimpleNamespace(
            data=data, message=SimpleNamespace(message_id=message_id, reply_text=AsyncMock(return_value=SimpleNamespace(message_id=789))),
            answer=AsyncMock(),
            edit_message_text=AsyncMock(), edit_message_reply_markup=AsyncMock(),
        )
        update = SimpleNamespace(
            effective_user=self.update.effective_user, effective_chat=self.update.effective_chat,
            effective_message=query.message, callback_query=query,
        )
        return query, update

    async def idle_message(self, *, hours: float = 2):
        """A text message after idle time; returns the generation mock. The notice message gets id 55."""
        if hours:
            self.session.last_message_at = datetime.now(timezone.utc) - timedelta(hours=hours)
        with patch("src.telegram.bot._send_generation", new=AsyncMock()) as generation:
            await text_message(self.update, self.context)
            await _drain()
        record = self.session.pending_reroute
        if record is not None:
            await _offer_reroute(self.update, self.service, self.session, record)
        return generation

    # ------------------------------------------------------------------ /newchat

    async def test_newchat_shows_agents_and_cancel_then_creates_with_the_chosen_agent(self) -> None:
        await new_chat(self.update, self.context)
        buttons = self.markup_buttons()
        self.assertEqual(
            [button.text for button in buttons],
            ["✅ First (agent-a)", "Second (agent-b)", "✖ Cancel (keep current chat)"],
        )
        self.service.create_chat.assert_not_called()
        panel = self.context.user_data["panel"]
        panel.message_id = 55
        query, update = self.query(buttons[1].callback_data, 55)
        await panel_callback(update, self.context)
        self.service.create_chat.assert_called_once_with("test-user", "agent-b")
        self.assertEqual((self.session.agent_id, self.session.chat_id), ("agent-b", "chat-of-agent-b"))
        self.assertEqual(self.preferences["agent_id"], "agent-b")
        text = query.edit_message_text.await_args.args[0]
        self.assertIn("Second", text)
        self.assertIn("chat-of-agent-b", text)
        self.assertNotIn("panel", self.context.user_data)

    async def test_newchat_cancel_changes_nothing(self) -> None:
        await new_chat(self.update, self.context)
        buttons = self.markup_buttons()
        self.context.user_data["panel"].message_id = 55
        query, update = self.query(buttons[-1].callback_data, 55)
        await panel_callback(update, self.context)
        self.service.create_chat.assert_not_called()
        self.service.update_preferences.assert_not_called()
        self.assertEqual((self.session.agent_id, self.session.chat_id), ("agent-a", "old-chat"))
        self.assertIn("Cancelled", query.edit_message_text.await_args.args[0])
        self.assertNotIn("panel", self.context.user_data)

    async def test_newchat_without_a_chat_uses_a_plain_cancel_label(self) -> None:
        self.session.chat_id = None
        await new_chat(self.update, self.context)
        self.assertEqual(self.markup_buttons()[-1].text, "✖ Cancel")

    async def test_newchat_with_a_single_agent_creates_the_chat_directly(self) -> None:
        self.service.list_agents.return_value = [{"id": "agent-a", "name": "First"}]
        await new_chat(self.update, self.context)
        self.service.create_chat.assert_called_once_with("test-user", "agent-a")
        self.assertEqual(self.session.chat_id, "chat-of-agent-a")
        self.assertIn("New chat created", self.message.reply_text.await_args.args[0])
        self.assertNotIn("panel", self.context.user_data)

    # ------------------------------------------------------------------ idle auto-new-chat

    async def test_idle_message_offers_both_buttons_within_the_callback_limit(self) -> None:
        generation = await self.idle_message()
        generation.assert_called_once()
        self.assertEqual(self.session.chat_id, "chat-of-agent-a")
        buttons = self.markup_buttons()
        self.assertEqual([button.text for button in buttons], ["↩ Continue previous chat", "🔀 Change agent"])
        for button in buttons:
            self.assertLessEqual(len(button.callback_data.encode()), 64)
        record = self.session.pending_reroute
        self.assertEqual(
            (record.previous_chat_id, record.previous_agent_id, record.auto_chat_id, record.text),
            ("old-chat", "agent-a", "chat-of-agent-a", "hello there"),
        )

    async def test_no_buttons_without_idle_or_without_a_previous_chat(self) -> None:
        self.session.last_message_at = datetime.now(timezone.utc)
        with patch("src.telegram.bot._send_generation", new=AsyncMock()):
            await text_message(self.update, self.context)
        self.assertIsNone(self.session.pending_reroute)
        # first message of a session with no chat anywhere
        self.session.chat_id, self.session.last_message_at = None, None
        self.service.latest_chat.return_value = None
        with patch("src.telegram.bot._send_generation", new=AsyncMock()):
            await text_message(self.update, self.context)
        self.assertIsNone(self.session.pending_reroute)
        self.assertEqual(self.session.chat_id, "chat-of-agent-a")

    async def test_explicit_newchat_then_message_is_not_rerouteable(self) -> None:
        self.service.list_agents.return_value = [{"id": "agent-a", "name": "First"}]
        await new_chat(self.update, self.context)
        with patch("src.telegram.bot._send_generation", new=AsyncMock()):
            await text_message(self.update, self.context)
        self.assertIsNone(self.session.pending_reroute)

    async def test_first_message_after_restart_offers_the_latest_chat_of_any_agent(self) -> None:
        self.session.chat_id, self.session.last_message_at = None, None
        self.service.latest_chat.return_value = {"id": "x1", "agent_id": "agent-b", "title": "Latest", "turns": 2}
        await self.idle_message(hours=0)
        record = self.session.pending_reroute
        self.assertEqual((record.previous_chat_id, record.previous_agent_id), ("x1", "agent-b"))

    async def test_continue_previous_replays_the_message_and_deletes_the_auto_chat(self) -> None:
        await self.idle_message()
        buttons = self.markup_buttons()
        query, update = self.query(buttons[0].callback_data, 55)
        with patch("src.telegram.bot._send_generation", new=AsyncMock()) as generation:
            await reroute_callback(update, self.context)
            await _drain()
        self.service.discard_chat.assert_called_once_with("test-user", "agent-a", "chat-of-agent-a")
        self.assertEqual((self.session.agent_id, self.session.chat_id), ("agent-a", "old-chat"))
        self.assertEqual(generation.await_args.args[2:4], ("hello there", []))
        self.assertFalse(generation.await_args.kwargs["new_chat_notice"])
        self.assertIn("Old topic", query.edit_message_text.await_args.args[0])
        self.assertIsNone(self.session.pending_reroute)
        # a second tap is told the option expired
        again, update = self.query(buttons[0].callback_data, 55)
        await reroute_callback(update, self.context)
        self.assertIn("expired", again.answer.await_args.args[0])
        self.service.discard_chat.assert_called_once()

    async def test_continue_switches_to_the_previous_chats_agent(self) -> None:
        self.session.chat_id, self.session.last_message_at = None, None
        self.service.latest_chat.return_value = {"id": "x1", "agent_id": "agent-b", "title": "Latest", "turns": 2}
        self.service.chat_summary.return_value = {"id": "x1", "title": "Latest", "turns": 2}
        await self.idle_message(hours=0)
        query, update = self.query(self.markup_buttons()[0].callback_data, 55)
        with patch("src.telegram.bot._send_generation", new=AsyncMock()):
            await reroute_callback(update, self.context)
            await _drain()
        self.assertEqual((self.session.agent_id, self.session.chat_id), ("agent-b", "x1"))
        self.assertEqual(self.preferences["agent_id"], "agent-b")
        self.service.chat_summary.assert_called_with("test-user", "agent-b", "x1")

    async def test_change_agent_opens_picker_then_replays_in_a_fresh_chat(self) -> None:
        await self.idle_message()
        query, update = self.query(self.markup_buttons()[1].callback_data, 55)
        await reroute_callback(update, self.context)
        markup = query.message.reply_text.await_args.kwargs["reply_markup"]
        buttons = [button for row in markup.inline_keyboard for button in row]
        self.assertEqual(buttons[-1].text, "✖ Cancel (keep current chat)")
        self.service.discard_chat.assert_not_called()
        self.assertIsNotNone(self.session.pending_reroute)
        self.context.user_data["panel"].message_id = 55
        picked, update = self.query(buttons[1].callback_data, 55)
        with patch("src.telegram.bot._send_generation", new=AsyncMock()) as generation:
            await panel_callback(update, self.context)
            await _drain()
        self.service.discard_chat.assert_called_once_with("test-user", "agent-a", "chat-of-agent-a")
        self.service.create_chat.assert_called_with("test-user", "agent-b")
        self.assertEqual((self.session.agent_id, self.session.chat_id), ("agent-b", "chat-of-agent-b"))
        self.assertEqual(generation.await_args.args[2:4], ("hello there", []))
        self.assertTrue(generation.await_args.kwargs["new_chat_notice"])
        self.assertIsNone(self.session.pending_reroute)

    async def test_expired_buttons_are_refused_and_nothing_is_deleted(self) -> None:
        await self.idle_message()
        data = self.markup_buttons()[0].callback_data
        self.session.pending_reroute.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        query, update = self.query(data, 55)
        await reroute_callback(update, self.context)
        self.assertIn("expired", query.answer.await_args.args[0])
        query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)
        self.service.discard_chat.assert_not_called()
        self.assertIsNone(self.session.pending_reroute)

    async def test_sending_another_message_expires_the_buttons(self) -> None:
        await self.idle_message()
        data = self.markup_buttons()[0].callback_data
        record = self.session.pending_reroute
        record.attachments = [{"name": "a"}]
        with patch("src.telegram.bot._send_generation", new=AsyncMock()):
            await text_message(self.update, self.context)
        self.assertIsNone(self.session.pending_reroute)
        self.assertEqual(record.attachments, [])
        self.context.bot.edit_message_reply_markup.assert_awaited_with(chat_id=456, message_id=55, reply_markup=None)
        query, update = self.query(data, 55)
        await reroute_callback(update, self.context)
        self.assertIn("expired", query.answer.await_args.args[0])
        self.service.discard_chat.assert_not_called()

    async def test_unusable_previous_chat_leaves_everything_untouched(self) -> None:
        from src.service.chat import UnknownChat
        await self.idle_message()
        self.service.chat_summary.side_effect = UnknownChat("unknown chat: {chat_id}", chat_id="old-chat")
        query, update = self.query(self.markup_buttons()[0].callback_data, 55)
        with self.assertLogs("recall.telegram", level="WARNING"):
            await reroute_callback(update, self.context)
        self.assertTrue(query.answer.await_args.kwargs["show_alert"])
        self.service.discard_chat.assert_not_called()
        self.assertEqual(self.session.chat_id, "chat-of-agent-a")
        self.assertIsNotNone(self.session.pending_reroute)

    async def test_generation_offers_buttons_only_for_idle_new_chat(self) -> None:
        await self.idle_message_with_real_generation()
        self.message.reply_text.assert_awaited()
        self.assertEqual(self.session.pending_reroute.message_id, 55)

    async def idle_message_with_real_generation(self) -> None:
        self.session.last_message_at = datetime.now(timezone.utc) - timedelta(hours=2)
        self.service.send.return_value = iter([ChatEvent("text", text="ok")])
        stream = Mock(start=AsyncMock(), update=AsyncMock(), finish=AsyncMock())
        with patch("src.telegram.bot._ReplyStream", return_value=stream):
            await text_message(self.update, self.context)
            await _drain()


async def _drain() -> None:
    import asyncio
    for _ in range(5):
        await asyncio.sleep(0)


if __name__ == "__main__":
    unittest.main()
