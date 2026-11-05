from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.config.preferences import UserPreferences
from src.config.settings import parse_chat_idle_minutes
from src.service.chat import ChatEvent
from src.telegram.bot import (
    TelegramBotState, TelegramSession, new_chat, panel_callback,
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
        self.service.default_agent.side_effect = lambda user: self.preferences.get("default_agent_id") or "agent-a"
        self.service.create_chat.side_effect = lambda user, agent, **kwargs: {"id": f"chat-of-{agent}"}
        self.service.chat_summary.return_value = {"id": "old-chat", "title": "Old topic", "turns": 4}
        self.service.chat_active.return_value = False
        self.service.get_chat.return_value = {"model": {"id": "llm/base", "provider": "OpenRouter"}, "model_source": "agent"}
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
        self.context.bot.edit_message_text = AsyncMock()
        grace = patch("src.telegram.bot._REROUTE_GRACE_SECONDS", 30)  # long: tests fire the timer themselves
        grace.start()
        self.addCleanup(grace.stop)

    @staticmethod
    def ago(**delta) -> str:
        return (datetime.now(timezone.utc) - timedelta(**delta)).isoformat(timespec="seconds")

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

    async def idle_message(self, *, hours: float = 2, grace: float | None = None):
        """A text message after idle time; returns the generation mock. The buttons message gets id 55."""
        if hours:
            self.session.last_message_at = datetime.now(timezone.utc) - timedelta(hours=hours)
        patches = [patch("src.telegram.bot._send_generation", new=AsyncMock())]
        if grace is not None:
            patches.append(patch("src.telegram.bot._REROUTE_GRACE_SECONDS", grace))
        with patches[0] as generation:
            for extra in patches[1:]:
                extra.start()
                self.addCleanup(extra.stop)
            await text_message(self.update, self.context)
            await _drain()
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
        self.service.create_chat.assert_called_once_with("test-user", "agent-b", origin="telegram")
        self.assertEqual((self.session.agent_id, self.session.chat_id), ("agent-b", "chat-of-agent-b"))
        self.assertEqual(self.preferences["agent_id"], "agent-b")
        text = query.edit_message_text.await_args.args[0]
        self.assertIn("Second", text)
        self.assertNotIn("chat-of-agent-b", text)
        self.assertNotIn("panel", self.context.user_data)
        entities = query.edit_message_text.await_args.kwargs["entities"]
        self.assertEqual(self.formatted(text, entities, "bold"), ["Second"])
        self.assertEqual(self.formatted(text, entities, "italic"), ["OpenRouter · llm/base"])

    @staticmethod
    def formatted(text: str, entities, kind: str) -> list[str]:
        """The text covered by the entities of `kind` (offsets are UTF-16 units)."""
        raw = text.encode("utf-16-le")
        return [
            raw[e.offset * 2:(e.offset + e.length) * 2].decode("utf-16-le") for e in entities if e.type == kind
        ]

    async def test_selectagent_is_the_picker_and_a_pick_never_resumes_the_old_agent_chat(self) -> None:
        from src.telegram.bot import _prepare_message_session
        await new_chat(self.update, self.context)  # /selectagent runs the same handler as /newchat
        self.context.user_data["panel"].message_id = 55
        query, update = self.query(self.markup_buttons()[1].callback_data, 55)
        await panel_callback(update, self.context)
        self.assertEqual((self.session.agent_id, self.session.chat_id), ("agent-b", "chat-of-agent-b"))
        self.assertIsNotNone(self.session.last_message_at)
        # a restart creates a fresh session from the saved preference: the latest chat is the new one
        self.service.latest_chat.return_value = {
            "id": "chat-of-agent-b", "agent_id": "agent-b", "title": "x", "turns": 0, "updated": self.ago(seconds=5),
        }
        restarted = TelegramSession("test-user", self.preferences["agent_id"])
        _prepare_message_session(restarted, self.service)
        self.assertEqual((restarted.agent_id, restarted.chat_id), ("agent-b", "chat-of-agent-b"))

    async def test_confirmation_markup_survives_special_characters_in_names_and_models(self) -> None:
        self.service.list_agents.return_value = [{"id": "agent-a", "name": "my_*agent*_ [x]"}]
        self.service.get_chat.return_value = {
            "model": {"id": "llm/a_b*c", "provider": "OpenRouter"}, "model_source": "agent",
        }
        await new_chat(self.update, self.context)
        call = self.message.reply_text.await_args
        self.assertEqual(self.formatted(call.args[0], call.kwargs["entities"], "bold"), ["my_*agent*_ [x]"])
        self.assertEqual(self.formatted(call.args[0], call.kwargs["entities"], "italic"), ["OpenRouter · llm/a_b*c"])

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
        self.service.create_chat.assert_called_once_with("test-user", "agent-a", origin="telegram")
        self.assertEqual(self.session.chat_id, "chat-of-agent-a")
        text = self.message.reply_text.await_args.args[0]
        self.assertIn("New chat created with First.", text)
        self.assertIn("Model: OpenRouter · llm/base (Agent default)", text)
        entities = self.message.reply_text.await_args.kwargs["entities"]
        self.assertEqual(self.formatted(text, entities, "bold"), ["First"])
        self.assertEqual(self.formatted(text, entities, "italic"), ["OpenRouter · llm/base"])
        self.assertIn("Agent default", text)
        self.assertNotIn("choose a model", text)
        self.assertNotIn("chat-of", text)
        self.assertNotIn("panel", self.context.user_data)

    # ------------------------------------------------------------------ idle auto-new-chat

    async def test_idle_message_offers_both_buttons_within_the_callback_limit(self) -> None:
        generation = await self.idle_message()
        generation.assert_not_called()  # nothing is generated during the grace period
        self.assertEqual(self.session.chat_id, "chat-of-agent-a")
        text = self.message.reply_text.await_args.args[0]
        self.assertIn("It has been a while. Starting a new chat with First.", text)
        self.assertIn("30 seconds", text)
        buttons = self.markup_buttons()
        self.assertEqual([button.text for button in buttons], ["✅ Use First", "↩ Continue previous chat", "🔀 Change agent"])
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
        self.service.latest_chat.return_value = {
            "id": "x1", "agent_id": "agent-b", "title": "Latest", "turns": 2, "updated": self.ago(hours=5),
        }
        await self.idle_message(hours=0)
        record = self.session.pending_reroute
        self.assertEqual((record.previous_chat_id, record.previous_agent_id), ("x1", "agent-b"))

    async def test_continue_previous_replays_the_message_and_deletes_the_auto_chat(self) -> None:
        await self.idle_message()
        buttons = self.markup_buttons()
        query, update = self.query(buttons[1].callback_data, 55)
        with patch("src.telegram.bot._send_generation", new=AsyncMock()) as generation:
            await reroute_callback(update, self.context)
            await _drain()
        self.service.discard_chat.assert_called_once_with("test-user", "agent-a", "chat-of-agent-a")
        self.assertEqual((self.session.agent_id, self.session.chat_id), ("agent-a", "old-chat"))
        self.assertEqual(generation.await_args.args[2:4], ("hello there", []))
        self.assertIn("Old topic", query.edit_message_text.await_args.args[0])
        self.assertIsNone(self.session.pending_reroute)
        # a second tap is told the option expired
        again, update = self.query(buttons[1].callback_data, 55)
        await reroute_callback(update, self.context)
        self.assertIn("expired", again.answer.await_args.args[0])
        self.service.discard_chat.assert_called_once()

    async def test_continue_switches_to_the_previous_chats_agent(self) -> None:
        self.session.chat_id, self.session.last_message_at = None, None
        self.service.latest_chat.return_value = {
            "id": "x1", "agent_id": "agent-b", "title": "Latest", "turns": 2, "updated": self.ago(hours=5),
        }
        self.service.chat_summary.return_value = {"id": "x1", "title": "Latest", "turns": 2}
        await self.idle_message(hours=0)
        query, update = self.query(self.markup_buttons()[1].callback_data, 55)
        with patch("src.telegram.bot._send_generation", new=AsyncMock()):
            await reroute_callback(update, self.context)
            await _drain()
        self.assertEqual((self.session.agent_id, self.session.chat_id), ("agent-b", "x1"))
        self.assertEqual(self.preferences["agent_id"], "agent-b")
        self.service.chat_summary.assert_called_with("test-user", "agent-b", "x1")

    async def test_change_agent_opens_picker_then_replays_in_a_fresh_chat(self) -> None:
        await self.idle_message()
        query, update = self.query(self.markup_buttons()[2].callback_data, 55)
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
        self.service.create_chat.assert_called_with("test-user", "agent-b", origin="telegram")
        self.assertEqual((self.session.agent_id, self.session.chat_id), ("agent-b", "chat-of-agent-b"))
        self.assertEqual(generation.await_args.args[2:4], ("hello there", []))
        self.assertIsNone(self.session.pending_reroute)

    async def test_use_button_accepts_the_proposed_agent_at_once_and_cancels_the_timer(self) -> None:
        await self.idle_message()
        record = self.session.pending_reroute
        use = self.markup_buttons()[0]
        self.assertEqual(use.text, "✅ Use First")
        self.assertLessEqual(len(use.callback_data.encode()), 64)
        query, update = self.query(use.callback_data, 55)
        with patch("src.telegram.bot._send_generation", new=AsyncMock()) as generation:
            await reroute_callback(update, self.context)
            await _drain()
            generation.assert_called_once()
            self.assertEqual(generation.await_args.args[2:4], ("hello there", []))
            self.assertTrue(record.timer.cancelled() or record.timer.done())
            self.assertIsNone(self.session.pending_reroute)
            self.assertEqual(self.session.chat_id, "chat-of-agent-a")  # the auto chat stays: nothing is discarded
            self.service.discard_chat.assert_not_called()
            # a second tap (or the timer firing at the same time) cannot start another generation
            again, update = self.query(use.callback_data, 55)
            await reroute_callback(update, self.context)
            await _drain()
            generation.assert_called_once()

    async def test_tap_after_the_grace_period_is_expired(self) -> None:
        await self.idle_message(grace=0.01)
        data = self.markup_buttons()[1].callback_data
        with patch("src.telegram.bot._send_generation", new=AsyncMock()) as generation:
            await asyncio.sleep(0.1)
        query, update = self.query(data, 55)
        await reroute_callback(update, self.context)
        self.assertIn("expired", query.answer.await_args.args[0])
        query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)
        self.service.discard_chat.assert_not_called()
        self.assertEqual(self.session.chat_id, "chat-of-agent-a")

    async def test_grace_expiry_starts_one_generation_and_removes_the_keyboard(self) -> None:
        with patch("src.telegram.bot._send_generation", new=AsyncMock()) as generation:
            self.session.last_message_at = datetime.now(timezone.utc) - timedelta(hours=2)
            with patch("src.telegram.bot._REROUTE_GRACE_SECONDS", 0.05):
                await text_message(self.update, self.context)
                generation.assert_not_called()
                await asyncio.sleep(0.3)
        generation.assert_called_once()
        self.assertEqual(generation.await_args.args[2:4], ("hello there", []))
        edit = self.context.bot.edit_message_text.await_args.kwargs
        self.assertEqual((edit["message_id"], edit["reply_markup"]), (55, None))
        self.assertEqual(edit["text"], "Answering in a new chat with First.")
        self.assertIsNone(self.session.pending_reroute)
        self.assertEqual(self.session.chat_id, "chat-of-agent-a")
        self.service.discard_chat.assert_not_called()

    async def test_new_message_during_grace_starts_the_pending_one_first(self) -> None:
        generation = await self.idle_message()
        record = self.session.pending_reroute
        self.message.text = "second"
        with patch("src.telegram.bot._send_generation", new=AsyncMock()) as generation:
            await text_message(self.update, self.context)
            await _drain()
        self.assertEqual([call.args[2] for call in generation.await_args_list], ["hello there", "second"])
        self.assertIsNone(self.session.pending_reroute)
        self.assertEqual(self.session.chat_id, "chat-of-agent-a")
        self.assertTrue(record.timer.cancelled() or record.timer.done())
        self.service.discard_chat.assert_not_called()

    # ------------------------------------------------------------------ resume from real activity

    async def test_first_message_after_restart_within_timeout_resumes_silently(self) -> None:
        self.session.chat_id, self.session.last_message_at = None, None
        self.service.latest_chat.return_value = {
            "id": "x1", "agent_id": "agent-b", "title": "Latest", "turns": 2, "updated": self.ago(minutes=1),
        }
        with patch("src.telegram.bot._send_generation", new=AsyncMock()) as generation:
            await text_message(self.update, self.context)
            await _drain()
        self.assertEqual((self.session.agent_id, self.session.chat_id), ("agent-b", "x1"))
        self.assertEqual(self.preferences["agent_id"], "agent-b")
        self.assertIsNone(self.session.pending_reroute)
        self.message.reply_text.assert_not_called()
        self.service.create_chat.assert_not_called()
        generation.assert_called_once()

    async def test_restart_resume_counts_a_running_reply_as_active(self) -> None:
        self.session.chat_id, self.session.last_message_at = None, None
        self.service.latest_chat.return_value = {
            "id": "x1", "agent_id": "agent-a", "title": "Long", "turns": 2, "updated": self.ago(hours=5),
        }
        self.service.chat_active.return_value = True
        with patch("src.telegram.bot._send_generation", new=AsyncMock()):
            await text_message(self.update, self.context)
        self.assertEqual(self.session.chat_id, "x1")
        self.assertIsNone(self.session.pending_reroute)

    async def test_restart_resume_refused_when_older_than_the_timeout(self) -> None:
        self.session.chat_id, self.session.last_message_at = None, None
        self.service.latest_chat.return_value = {
            "id": "x1", "agent_id": "agent-b", "title": "Latest", "turns": 2, "updated": self.ago(minutes=61),
        }
        generation = await self.idle_message(hours=0)
        generation.assert_not_called()
        self.assertEqual(self.session.chat_id, "chat-of-agent-a")
        self.assertEqual(self.session.pending_reroute.previous_chat_id, "x1")
        self.assertEqual(len(self.markup_buttons()), 3)

    async def test_persisted_activity_newer_than_memory_keeps_the_chat(self) -> None:
        self.session.last_message_at = datetime.now(timezone.utc) - timedelta(hours=3)
        self.service.chat_summary.return_value = {"id": "old-chat", "title": "T", "turns": 4, "updated": self.ago(minutes=2)}
        with patch("src.telegram.bot._send_generation", new=AsyncMock()):
            await text_message(self.update, self.context)
        self.assertEqual(self.session.chat_id, "old-chat")
        self.assertIsNone(self.session.pending_reroute)

    async def test_custom_idle_timeout_is_honoured(self) -> None:
        self.session.last_message_at = datetime.now(timezone.utc) - timedelta(seconds=30)
        with patch("src.telegram.bot._CHAT_IDLE_TIMEOUT", timedelta(seconds=10)):
            generation = await self.idle_message(hours=0)
        generation.assert_not_called()
        self.assertEqual(self.session.chat_id, "chat-of-agent-a")

    def test_idle_minutes_setting_and_env_override(self) -> None:
        self.assertEqual(parse_chat_idle_minutes(None), 60.0)
        self.assertEqual(parse_chat_idle_minutes(1), 1.0)
        self.assertEqual(parse_chat_idle_minutes(10080), 10080.0)
        self.assertEqual(parse_chat_idle_minutes(30, "0.5"), 0.5)
        self.assertEqual(parse_chat_idle_minutes(30, "  "), 30.0)
        for bad in (0, 10081, -1, "60", 1.5, True):
            with self.assertRaises(ValueError, msg=repr(bad)):
                parse_chat_idle_minutes(bad)
        for bad in ("0", "-2", "abc", "inf", "nan"):
            with self.assertRaises(ValueError, msg=bad):
                parse_chat_idle_minutes(30, bad)

    async def test_unusable_previous_chat_leaves_everything_untouched(self) -> None:
        from src.service.chat import UnknownChat
        await self.idle_message()
        self.service.chat_summary.side_effect = UnknownChat("unknown chat: {chat_id}", chat_id="old-chat")
        query, update = self.query(self.markup_buttons()[1].callback_data, 55)
        with self.assertLogs("recall.telegram", level="WARNING"):
            await reroute_callback(update, self.context)
        self.assertTrue(query.answer.await_args.kwargs["show_alert"])
        self.service.discard_chat.assert_not_called()
        self.assertEqual(self.session.chat_id, "chat-of-agent-a")
        self.assertIsNotNone(self.session.pending_reroute)

    async def test_idle_message_with_real_generation_sends_buttons_then_answers_after_grace(self) -> None:
        self.session.last_message_at = datetime.now(timezone.utc) - timedelta(hours=2)
        self.service.send.return_value = iter([ChatEvent("text", text="ok")])
        stream = Mock(start=AsyncMock(), update=AsyncMock(), finish=AsyncMock())
        with patch("src.telegram.bot._ReplyStream", return_value=stream), \
                patch("src.telegram.bot._REROUTE_GRACE_SECONDS", 0.05):
            await text_message(self.update, self.context)
            self.assertEqual(self.session.pending_reroute.message_id, 55)
            self.service.send.assert_not_called()
            await asyncio.sleep(0.4)
        self.service.send.assert_called_once()
        self.assertIsNone(self.session.pending_reroute)


async def _drain() -> None:
    import asyncio
    for _ in range(5):
        await asyncio.sleep(0)


if __name__ == "__main__":
    unittest.main()
