from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from telegram import MessageEntity, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import ApplicationHandlerStop

from src.config.preferences import PreferencesError, UserPreferences
from src.service.chat import ChatEvent
from src.telegram.bot import (
    TelegramBotState, TelegramSession, _final_messages, _generation_message,
    _prepare_message_session, _send_generation, agent_selected, new_chat,
    refresh_language, settings_command, settings_selected, text_message,
)


class TelegramSettingsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.preferences = {"show_thinking": True, "language": "en", "agent_id": "agent-a"}
        self.service = Mock()
        self.service.get_preferences.side_effect = lambda user: self.preferences.copy()
        self.service.language.side_effect = lambda user: self.preferences["language"]
        self.service.list_agents.return_value = [
            {"id": "agent-a", "name": "First"}, {"id": "agent-b", "name": "Second"},
        ]
        self.service.create_chat.return_value = {"id": "new-chat"}

        def save(user: str, changes: UserPreferences) -> dict:
            self.preferences.update(changes.model_dump(exclude_unset=True))
            return self.preferences.copy()

        self.service.update_preferences.side_effect = save
        self.session = TelegramSession("test-user", "agent-a", "current-chat")
        self.state = TelegramBotState(self.service, {123: self.session})
        self.update = SimpleNamespace(
            effective_user=SimpleNamespace(id=123),
            effective_chat=SimpleNamespace(id=456, type="private"),
            effective_message=SimpleNamespace(text="", reply_text=AsyncMock()),
        )
        self.context = SimpleNamespace(
            bot=Mock(), application=SimpleNamespace(bot_data={"state": self.state}),
            user_data={}, args=[],
        )

    async def choose(self, data: str) -> None:
        if "settings_options" not in self.context.user_data:
            await settings_command(self.update, self.context)
        self.update.effective_message.text = next(
            label for label, action in self.context.user_data["settings_options"].items()
            if action == data
        )
        self.assertTrue(await settings_selected(self.update, self.context))

    async def test_settings_menu_shows_shared_values_and_buttons(self) -> None:
        await settings_command(self.update, self.context)
        call = self.update.effective_message.reply_text.await_args
        self.assertIn("Thinking: visible", call.args[0])
        self.assertIn("Active agent: First", call.args[0])
        self.assertIsInstance(call.kwargs["reply_markup"], ReplyKeyboardMarkup)
        labels = [button.text for row in call.kwargs["reply_markup"].keyboard for button in row]
        self.assertEqual(labels, ["Thinking: visible", "✅ English", "Español", "Active agent", "Close"])

    async def test_settings_agent_reuses_selector_and_changes_active_agent(self) -> None:
        await self.choose("thinking")
        self.assertFalse(self.preferences["show_thinking"])
        await self.choose("language:es")
        self.assertEqual(self.preferences["language"], "es")
        self.assertEqual(self.session.chat_id, "current-chat")
        await self.choose("agents")
        self.assertNotIn("settings_options", self.context.user_data)
        self.assertIsInstance(
            self.update.effective_message.reply_text.await_args.kwargs["reply_markup"], ReplyKeyboardMarkup,
        )
        self.update.effective_message.text = "Second (agent-b)"
        self.assertTrue(await agent_selected(self.update, self.context))
        self.assertEqual(self.preferences["agent_id"], "agent-b")
        self.assertEqual(self.session.agent_id, "agent-b")
        self.assertIsNone(self.session.chat_id)
        self.service.send.assert_not_called()

    async def test_unknown_text_is_not_treated_as_a_settings_choice(self) -> None:
        await settings_command(self.update, self.context)
        self.update.effective_message.text = "Normal chat message"
        self.assertFalse(await settings_selected(self.update, self.context))
        self.service.update_preferences.assert_not_called()

    async def test_closing_settings_removes_keyboard_and_menu_state(self) -> None:
        await self.choose("close")
        self.assertNotIn("settings_options", self.context.user_data)
        self.assertIsInstance(
            self.update.effective_message.reply_text.await_args.kwargs["reply_markup"], ReplyKeyboardRemove,
        )

    async def test_settings_shows_active_agent_instead_of_saved_new_chat_default(self) -> None:
        self.preferences["agent_id"] = "agent-b"
        await settings_command(self.update, self.context)
        self.assertIn("Active agent: First", self.update.effective_message.reply_text.await_args.args[0])

    async def test_text_dispatch_consumes_settings_without_sending_to_model(self) -> None:
        await settings_command(self.update, self.context)
        self.update.effective_message.text = "Active agent"
        await text_message(self.update, self.context)
        self.assertIn("Second (agent-b)", self.context.user_data["agent_options"])
        self.service.send.assert_not_called()
        self.service.create_chat.assert_not_called()
        self.update.effective_message.text = "Second (agent-b)"
        await text_message(self.update, self.context)
        self.assertEqual(self.session.agent_id, "agent-b")
        self.assertIsNone(self.session.chat_id)
        self.service.send.assert_not_called()

    async def test_settings_replaces_previous_selection_state(self) -> None:
        self.context.user_data.update({
            "agent_options": {"Old": "agent-b"}, "model_options": {"Old": "model"},
            "stt_options": {"Old": "stt"},
        })
        await settings_command(self.update, self.context)
        for key in ("agent_options", "model_options", "stt_options"):
            self.assertNotIn(key, self.context.user_data)

    async def test_failed_save_is_explicit_and_does_not_change_session(self) -> None:
        self.service.update_preferences.side_effect = PreferencesError("Could not save preferences")
        with self.assertLogs("recall.telegram", level="WARNING"):
            await self.choose("thinking")
        self.assertIn("Could not save preferences", self.update.effective_message.reply_text.await_args.args[0])
        self.assertEqual(self.session.chat_id, "current-chat")

    async def test_unauthorized_settings_are_ignored(self) -> None:
        self.update.effective_user.id = 999
        await settings_command(self.update, self.context)
        self.context.user_data["settings_options"] = {"Thinking": "thinking"}
        self.update.effective_message.text = "Thinking"
        self.assertTrue(await settings_selected(self.update, self.context))
        self.service.get_preferences.assert_not_called()
        self.service.update_preferences.assert_not_called()
        self.update.effective_message.reply_text.assert_not_awaited()

    async def test_newchat_reads_preferred_agent_saved_by_another_channel(self) -> None:
        self.preferences["agent_id"] = "agent-b"
        await new_chat(self.update, self.context)
        self.service.create_chat.assert_called_once_with("test-user", "agent-b")
        self.assertEqual((self.session.agent_id, self.session.chat_id), ("agent-b", "new-chat"))

    async def test_current_chat_stays_on_its_agent_until_idle_timeout(self) -> None:
        self.preferences["agent_id"] = "agent-b"
        self.session.last_message_at = datetime.now(timezone.utc)
        self.assertFalse(_prepare_message_session(self.session, self.service))
        self.service.get_preferences.assert_not_called()
        self.service.create_chat.assert_not_called()
        self.assertEqual(self.session.agent_id, "agent-a")
        self.session.last_message_at -= timedelta(hours=2)
        self.assertTrue(_prepare_message_session(self.session, self.service))
        self.service.create_chat.assert_called_once_with("test-user", "agent-b")
        self.assertEqual(self.session.agent_id, "agent-b")

    async def test_explicit_selectagent_still_switches_and_persists_shared_selection(self) -> None:
        self.context.user_data["agent_options"] = {"Second": "agent-b"}
        self.update.effective_message.text = "Second"
        self.assertTrue(await agent_selected(self.update, self.context))
        self.assertEqual(self.preferences["agent_id"], "agent-b")
        self.assertEqual(self.session.agent_id, "agent-b")
        self.assertIsNone(self.session.chat_id)

    async def test_hidden_thinking_does_not_remove_tools_or_interrupted_text(self) -> None:
        self.preferences["show_thinking"] = False
        action = {"name": "query", "arguments": {}}
        self.service.send.return_value = iter([
            ChatEvent("reasoning", "Private thinking"),
            ChatEvent("tool", data=action),
            ChatEvent("interrupted", "Partial answer", data={
                "reasoning": "Private thinking", "actions": [action],
            }),
        ])
        stream = AsyncMock()
        with patch("src.telegram.bot._ReplyStream", return_value=stream):
            await _send_generation(self.update, self.context, "Hello", [])
        for call in stream.update.await_args_list:
            self.assertNotIn("Private thinking", call.args[0])
        final = stream.finish.await_args.args[0]
        text = "".join(part for part, entities in final)
        self.assertNotIn("Private thinking", text)
        self.assertEqual(text.count(">query()"), 1)
        self.assertIn("Partial answer", text)
        for part, entities in final:
            self.assertFalse(any(entity.type == MessageEntity.EXPANDABLE_BLOCKQUOTE for entity in entities))

    async def test_generation_keeps_original_route_when_session_changes_during_start(self) -> None:
        async def change_session() -> None:
            self.session.agent_id = "agent-b"
            self.session.chat_id = "another-chat"

        stream = AsyncMock()
        stream.start.side_effect = change_session
        self.service.send.return_value = iter([ChatEvent("text", "Answer")])
        with patch("src.telegram.bot._ReplyStream", return_value=stream):
            await _send_generation(self.update, self.context, "Hello", [])
        self.assertEqual(self.service.send.call_args.args[:3], ("test-user", "agent-a", "current-chat"))

    async def test_language_selection_translates_the_settings_menu_immediately(self) -> None:
        await self.choose("language:es")
        self.assertIn("Agente activo", self.update.effective_message.reply_text.await_args.args[0])
        self.assertEqual(self.session.language, "es")
        await self.choose("language:en")
        self.assertIn("Active agent", self.update.effective_message.reply_text.await_args.args[0])
        self.assertEqual(self.session.language, "en")

    async def test_language_middleware_picks_up_web_changes_without_switching_chat(self) -> None:
        self.preferences["language"] = "es"
        await refresh_language(self.update, self.context)
        self.assertEqual(self.session.language, "es")
        self.assertEqual(self.session.chat_id, "current-chat")

    async def test_language_read_failure_notifies_in_last_known_language_and_stops(self) -> None:
        self.session.language = "es"
        self.service.language.side_effect = PreferencesError("Could not read preferences")
        with self.assertLogs("recall.telegram", level="WARNING"):
            with self.assertRaises(ApplicationHandlerStop):
                await refresh_language(self.update, self.context)
        self.assertIn("No se han podido cargar los ajustes", self.update.effective_message.reply_text.await_args.args[0])

    async def test_generation_failure_translates_wrapper_but_keeps_vendor_text(self) -> None:
        self.preferences["language"] = "es"
        original = "ORIGINAL vendor error {raw}"
        self.service.send.side_effect = RuntimeError(original)
        stream = AsyncMock()
        with patch("src.telegram.bot._ReplyStream", return_value=stream):
            with self.assertLogs("recall.telegram", level="ERROR"):
                await _send_generation(self.update, self.context, "Hello", [])
        self.assertEqual(stream.finish.await_args.args[0][0][0], f"La generación ha fallado: {original}")


class ThinkingVisibilityTests(unittest.TestCase):
    def test_hidden_thinking_preserves_tool_timeline_and_long_final_messages(self) -> None:
        parts = [
            ("reasoning", "Hidden"), ("reasoning_tool", ">first()"),
            ("text", "a" * 5000), ("reasoning", "Also hidden"),
            ("reasoning_tool", ">second()"), ("text", "Final"),
        ]
        text, entities = _generation_message(parts, show_thinking=False)
        self.assertNotIn("Hidden", text)
        self.assertNotIn("Also hidden", text)
        self.assertLess(text.index(">first()"), text.index(">second()"))
        self.assertTrue(text.endswith("Final"))
        self.assertFalse(any(entity.type == MessageEntity.EXPANDABLE_BLOCKQUOTE for entity in entities))
        self.assertEqual(
            "".join(text for text, entities in _final_messages(parts, show_thinking=False)), text,
        )


if __name__ == "__main__":
    unittest.main()
