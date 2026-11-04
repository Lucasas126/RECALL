from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from telegram import InlineKeyboardMarkup, MessageEntity
from telegram.error import BadRequest
from telegram.ext import ApplicationHandlerStop

from src.config.preferences import PreferencesError, UserPreferences
from src.service.chat import AgentBusy, ChatEvent
from src.telegram.bot import (
    TelegramBotState, TelegramSession, _edit_panel, _final_messages, _generation_message,
    _prepare_message_session, _send_generation, new_chat, panel_callback,
    refresh_language, select_model, settings_command, text_message,
)


class TelegramSettingsTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.preferences = {
            "show_thinking": True, "language": "en", "agent_id": "agent-a",
            "default_llm_model": "llm/base", "default_stt_model": "stt/base",
        }
        self.service = Mock()
        self.service.get_preferences.side_effect = lambda user: self.preferences.copy()
        self.service.language.side_effect = lambda user: self.preferences["language"]
        self.service.list_agents.return_value = [
            {"id": "agent-a", "name": "First"}, {"id": "agent-b", "name": "Second"},
        ]
        self.service.create_chat.return_value = {"id": "new-chat"}
        self.service.get_chat.return_value = {"model": {"id": "llm/base", "provider": "OpenRouter"}, "model_source": "agent"}
        self.service.get_chat_models.return_value = {
            "model": {"id": "llm/base", "zdr": True}, "model_override": None,
            "stt_model": "stt/base", "stt_model_override": None,
        }
        self.service.list_favorite_models.return_value = ["llm/favorite", "llm/second"]
        self.service.list_models.return_value = [
            {"id": "llm/cheap", "name": "Cheap", "pricing": {"prompt": "0", "completion": "0"}},
            {"id": "llm/favorite", "name": "Favorite", "pricing": {"prompt": "2", "completion": "3"}},
            {"id": "llm/second", "name": "Second", "pricing": {"prompt": "5", "completion": "6"}},
        ]
        self.service.list_transcription_models.return_value = [
            {"id": "stt/new", "name": "New STT", "pricing_prompt": "0.1"},
        ]

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
        self.context.bot.delete_message = AsyncMock()

    async def open_panel(self, command=settings_command):
        self.update.effective_message.reply_text.return_value = SimpleNamespace(message_id=789)
        await command(self.update, self.context)
        call = self.update.effective_message.reply_text.await_args
        self.markup = call.kwargs["reply_markup"]
        self.text = call.args[0]
        return call

    def action(self, label: str) -> str:
        button = next(
            button for row in self.markup.inline_keyboard for button in row if label in button.text
        )
        return button.callback_data.split(":", 2)[2]

    async def press(self, action: str, *, token: str | None = None, message_id: int = 789):
        if "panel" not in self.context.user_data:
            await self.open_panel()
        panel = self.context.user_data.get("panel")
        query = SimpleNamespace(
            data=f"p:{token or panel.token}:{action}",
            message=SimpleNamespace(message_id=message_id),
            answer=AsyncMock(), edit_message_text=AsyncMock(), edit_message_reply_markup=AsyncMock(),
        )
        update = SimpleNamespace(
            effective_user=self.update.effective_user, effective_chat=self.update.effective_chat,
            effective_message=query.message, callback_query=query,
        )
        await panel_callback(update, self.context)
        if query.edit_message_text.await_args is not None:
            self.text = query.edit_message_text.await_args.args[0]
            self.markup = query.edit_message_text.await_args.kwargs["reply_markup"]
        return query

    async def test_settings_panel_shows_shared_values_with_inline_buttons(self) -> None:
        call = await self.open_panel()
        self.assertIn("Thinking: visible", call.args[0])
        self.assertIn("Active agent: First", call.args[0])
        self.assertIn("Default LLM: llm/base", call.args[0])
        self.assertIn("Default STT: stt/base", call.args[0])
        self.assertIsInstance(self.markup, InlineKeyboardMarkup)
        buttons = [button for row in self.markup.inline_keyboard for button in row]
        self.assertEqual([button.text for button in buttons], [
            "Thinking: visible", "✅ English", "Español", "Active agent",
            "Default LLM model", "Default STT model", "Chat reasoning effort", "Close",
        ])
        self.assertTrue(all(len(button.callback_data.encode()) <= 64 for button in buttons))

    async def test_toggles_edit_the_same_panel_without_sending_messages(self) -> None:
        await self.open_panel()
        query = await self.press("thinking")
        self.assertFalse(self.preferences["show_thinking"])
        query.answer.assert_awaited_once_with()
        self.assertIn("Thinking: hidden", self.text)
        await self.press("lang:es")
        self.assertEqual(self.session.language, "es")
        self.assertIn("Agente activo", self.text)
        self.assertIn("✅ Español", [button.text for row in self.markup.inline_keyboard for button in row])
        await self.press("lang:en")
        self.assertIn("Active agent", self.text)
        self.assertEqual(self.update.effective_message.reply_text.await_count, 2)
        self.assertEqual(self.session.chat_id, "current-chat")
        self.service.send.assert_not_called()

    async def test_defaults_are_user_scoped_without_requiring_a_chat(self) -> None:
        self.session.chat_id = None
        await self.open_panel()
        await self.press("llm")
        labels = [button.text for row in self.markup.inline_keyboard for button in row]
        self.assertTrue(labels[0].startswith("⭐ 1. OpenRouter · Favorite"))
        self.assertNotIn("Cheap", " ".join(labels))  # only favorites are offered
        self.assertNotIn("Use user default", labels)
        await self.press(self.action("Favorite"))
        self.assertEqual(self.preferences["default_llm_model"], "llm/favorite")
        self.assertIn("Default LLM: llm/favorite", self.text)
        await self.press("stt")
        await self.press(self.action("New STT"))
        self.assertEqual(self.preferences["default_stt_model"], "stt/new")
        self.assertIn("Default STT: stt/new", self.text)
        self.assertIsNone(self.session.chat_id)
        self.service.set_chat_model.assert_not_called()
        self.service.create_chat.assert_not_called()
        self.service.send.assert_not_called()
        self.assertEqual(self.update.effective_message.reply_text.await_count, 2)

    async def test_default_pagination_and_back_stay_in_the_panel(self) -> None:
        self.service.list_transcription_models.return_value = [
            {"id": f"stt/{index}", "name": f"STT {index}", "pricing_prompt": "0.1"}
            for index in range(11)
        ]
        await self.open_panel()
        await self.press("stt")
        self.assertIn("Page 1/2", self.text)
        await self.press(self.action("Next"))
        self.assertIn("Page 2/2", self.text)
        await self.press(self.action("Back"))
        self.assertIn("Settings", self.text)
        self.service.update_preferences.assert_not_called()
        await self.press("stt")
        await self.press("page:1")
        await self.press(self.action("STT 10"))
        self.assertEqual(self.preferences["default_stt_model"], "stt/10")
        self.assertEqual(self.update.effective_message.reply_text.await_count, 2)

    async def test_agent_choice_in_settings_starts_a_chat_with_that_agent(self) -> None:
        await self.open_panel()
        await self.press("agents")
        query = await self.press(self.action("Second (agent-b)"))
        self.assertEqual(self.preferences["agent_id"], "agent-b")
        self.assertEqual((self.session.agent_id, self.session.chat_id), ("agent-b", "new-chat"))
        self.service.create_chat.assert_called_once_with("test-user", "agent-b")
        self.assertIn("New chat created with Second.", self.text)
        entities = query.edit_message_text.await_args.kwargs["entities"]
        bold = next(e for e in entities if e.type == "bold")
        self.assertEqual(self.text[bold.offset:bold.offset + bold.length], "Second")
        self.assertNotIn("panel", self.context.user_data)
        self.service.send.assert_not_called()

    async def test_closing_removes_buttons_and_panel_state(self) -> None:
        await self.open_panel()
        query = await self.press("close")
        query.edit_message_text.assert_awaited_once_with("Settings closed.", reply_markup=None)
        self.assertNotIn("panel", self.context.user_data)
        query = await self.press("thinking", token="stale")
        query.answer.assert_awaited_once()
        self.assertTrue(query.answer.await_args.kwargs["show_alert"])
        self.service.update_preferences.assert_not_called()

    async def test_reopening_settings_expires_the_previous_panel(self) -> None:
        self.context.bot.delete_message = AsyncMock()
        self.context.bot.edit_message_reply_markup = AsyncMock()
        await self.open_panel()
        old = self.context.user_data["panel"]
        self.update.effective_message.reply_text.return_value = SimpleNamespace(message_id=790)
        await settings_command(self.update, self.context)
        self.context.bot.delete_message.assert_awaited()
        query = await self.press("thinking", token=old.token, message_id=789)
        self.assertIn("expired", query.answer.await_args.args[0])
        query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)
        self.service.update_preferences.assert_not_called()

    async def test_failed_save_alerts_without_editing_or_changing_session(self) -> None:
        await self.open_panel()
        self.service.update_preferences.side_effect = PreferencesError("Could not save preferences")
        with self.assertLogs("recall.telegram", level="WARNING"):
            query = await self.press("thinking")
        self.assertIn("Could not save preferences", query.answer.await_args.args[0])
        self.assertTrue(query.answer.await_args.kwargs["show_alert"])
        query.edit_message_text.assert_not_awaited()
        self.assertEqual(self.session.chat_id, "current-chat")

    async def test_unchanged_panel_edit_is_not_an_error(self) -> None:
        await self.open_panel()
        query = SimpleNamespace(edit_message_text=AsyncMock(side_effect=BadRequest("Message is not modified")))
        await _edit_panel(query, "Same", None)
        query.edit_message_text.side_effect = BadRequest("Message to edit not found")
        with self.assertRaises(BadRequest):
            await _edit_panel(query, "Same", None)

    async def test_unauthorized_callbacks_are_answered_and_ignored(self) -> None:
        await self.open_panel()
        self.update.effective_user = SimpleNamespace(id=999)
        self.service.get_preferences.reset_mock()
        query = await self.press("thinking")
        query.answer.assert_awaited_once_with()
        self.service.get_preferences.assert_not_called()
        self.service.update_preferences.assert_not_called()

    async def test_regular_text_after_settings_is_sent_to_the_model(self) -> None:
        await self.open_panel()
        self.update.effective_message.text = "Thinking: visible"
        with patch("src.telegram.bot._send_generation", new=AsyncMock()) as send:
            await text_message(self.update, self.context)
            await asyncio.sleep(0)
        send.assert_awaited_once()
        self.service.update_preferences.assert_not_called()
        self.assertIn("panel", self.context.user_data)

    async def test_settings_shows_active_agent_instead_of_saved_new_chat_default(self) -> None:
        self.preferences["agent_id"] = "agent-b"
        await self.open_panel()
        self.assertIn("Active agent: First", self.text)

    async def test_unauthorized_settings_command_is_ignored(self) -> None:
        self.update.effective_user.id = 999
        await settings_command(self.update, self.context)
        self.service.get_preferences.assert_not_called()
        self.update.effective_message.reply_text.assert_not_awaited()

    async def test_chat_model_selector_is_inline_and_only_changes_this_chat(self) -> None:
        await self.open_panel(select_model)
        self.assertIsInstance(self.markup, InlineKeyboardMarkup)
        labels = [button.text for row in self.markup.inline_keyboard for button in row]
        self.assertEqual(labels[0], "Use user default")
        self.assertTrue(labels[1].startswith("⭐ 1. OpenRouter · Favorite"))
        self.assertNotIn("Cheap", " ".join(labels))
        self.assertEqual(labels[-1], "Close")
        self.assertIn("User default", self.text)
        query = await self.press(self.action("Second"))
        self.service.set_chat_model.assert_called_once_with(
            "test-user", "agent-a", "current-chat", "llm/second",
        )
        query.edit_message_text.assert_awaited_once()
        self.assertIsNone(query.edit_message_text.await_args.kwargs["reply_markup"])
        self.assertIn("OpenRouter · llm/second", self.text)
        self.assertNotIn("panel", self.context.user_data)
        self.service.update_preferences.assert_not_called()
        self.assertEqual(self.update.effective_message.reply_text.await_count, 2)

    async def test_chat_selectors_reset_overrides_without_changing_defaults(self) -> None:
        before = self.preferences.copy()
        await self.open_panel(select_model)
        await self.press("inherit")
        self.assertEqual(
            (self.service.set_chat_model.call_args.args, self.service.set_chat_model.call_args.kwargs),
            (("test-user", "agent-a", "current-chat", None), {}),
        )
        self.assertIn("default", self.text)
        self.assertEqual(self.preferences, before)
        self.service.update_preferences.assert_not_called()
        self.service.send.assert_not_called()

    async def test_chat_selector_expires_when_the_current_chat_changes(self) -> None:
        await self.open_panel(select_model)
        self.session.chat_id = "another-chat"
        query = await self.press("pick:0")
        self.assertIn("expired", query.answer.await_args.args[0])
        self.service.set_chat_model.assert_not_called()

    async def test_busy_agent_is_reported_as_an_alert_and_keeps_the_selector(self) -> None:
        await self.open_panel(select_model)
        self.service.set_chat_model.side_effect = AgentBusy(
            "wait for this agent's active and queued replies before changing models",
        )
        with self.assertLogs("recall.telegram", level="WARNING"):
            query = await self.press("pick:0")
        self.assertTrue(query.answer.await_args.kwargs["show_alert"])
        self.assertIn("wait for this agent", query.answer.await_args.args[0])
        query.edit_message_text.assert_not_awaited()
        self.assertIn("panel", self.context.user_data)

    async def test_selectagent_picker_creates_a_chat_and_persists_the_agent(self) -> None:
        await self.open_panel(new_chat)  # /selectagent is registered on the /newchat handler
        await self.press(self.action("Second (agent-b)"))
        self.assertEqual(self.preferences["agent_id"], "agent-b")
        self.assertEqual((self.session.agent_id, self.session.chat_id), ("agent-b", "new-chat"))
        self.assertIn("Second", self.text)
        self.assertNotIn("panel", self.context.user_data)

    async def test_closing_a_selector_only_removes_its_buttons(self) -> None:
        await self.open_panel(select_model)
        query = await self.press("close")
        query.edit_message_reply_markup.assert_awaited_once_with(reply_markup=None)
        query.edit_message_text.assert_not_awaited()
        self.service.set_chat_model.assert_not_called()

    async def test_selector_rejects_settings_only_actions(self) -> None:
        await self.open_panel(select_model)
        with self.assertLogs("recall.telegram", level="WARNING"):
            query = await self.press("thinking")
        self.assertTrue(query.answer.await_args.kwargs["show_alert"])
        self.service.update_preferences.assert_not_called()

    async def test_newchat_reads_preferred_agent_saved_by_another_channel(self) -> None:
        self.preferences["agent_id"] = "agent-b"
        self.service.list_agents.return_value = [{"id": "agent-b", "name": "Second"}]  # one agent: no picker
        await new_chat(self.update, self.context)
        self.service.create_chat.assert_called_once_with("test-user", "agent-b")
        self.assertEqual((self.session.agent_id, self.session.chat_id), ("agent-b", "new-chat"))

    async def test_current_chat_stays_on_its_agent_until_idle_timeout(self) -> None:
        self.preferences["agent_id"] = "agent-b"
        self.session.last_message_at = datetime.now(timezone.utc)
        _prepare_message_session(self.session, self.service)
        self.service.get_preferences.assert_not_called()
        self.service.create_chat.assert_not_called()
        self.assertEqual(self.session.agent_id, "agent-a")
        self.session.last_message_at -= timedelta(hours=2)
        _prepare_message_session(self.session, self.service)
        self.service.create_chat.assert_called_once_with("test-user", "agent-b")
        self.assertEqual(self.session.agent_id, "agent-b")

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


    async def test_chat_reasoning_effort_is_chosen_from_settings_for_the_current_chat(self) -> None:
        self.service.get_chat_models.return_value = {
            "model": {"id": "llm/base", "zdr": True}, "model_override": None,
            "stt_model": "stt/base", "stt_model_override": None,
            "reasoning_effort": {
                "override": None, "default": "medium", "effective": "medium", "reasoning": True, "supported": True,
            },
        }
        await self.open_panel()
        await self.press(self.action("Chat reasoning effort"))
        self.assertIn("Choose the reasoning effort for this chat", self.text)
        labels = [button.text for row in self.markup.inline_keyboard for button in row]
        self.assertEqual(labels[0], "✅ Default (medium)")
        high = next(button for row in self.markup.inline_keyboard for button in row if button.text == "high")
        query = await self.press(high.callback_data.split(":", 2)[2])
        self.service.set_chat_reasoning_effort.assert_called_once_with("test-user", "agent-a", "current-chat", "high")
        self.assertIn("Reasoning effort for this chat: high", query.edit_message_text.await_args.args[0])

    async def test_chat_reasoning_effort_explains_unsupported_models_without_choices(self) -> None:
        self.service.get_chat_models.return_value = {
            "model": {"id": "llm/base", "zdr": True}, "model_override": None,
            "stt_model": "stt/base", "stt_model_override": None,
            "reasoning_effort": {
                "override": None, "default": "medium", "effective": "medium", "reasoning": True, "supported": False,
            },
        }
        await self.open_panel()
        await self.press(self.action("Chat reasoning effort"))
        self.assertIn("This model does not support reasoning effort.", self.text)
        labels = [button.text for row in self.markup.inline_keyboard for button in row]
        self.assertEqual(labels, ["Back"])
        self.service.set_chat_reasoning_effort.assert_not_called()


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
