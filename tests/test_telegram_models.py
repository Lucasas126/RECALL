from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from src.config.favorites import FavoritesError
from src.telegram.bot import (
    TelegramBotState, TelegramSession, _apply_panel_action, _llm_label, _Panel, _sort_models, _zdr_line, select_model,
    show_model,
)


class FavoriteSortingTests(unittest.TestCase):
    def test_favorites_precede_cheaper_models_without_mutating_catalog(self) -> None:
        models = [
            {"id": "vendor/free", "name": "Free", "pricing": {"prompt": "0", "completion": "0"}},
            {"id": "vendor/favorite", "name": "Favorite", "pricing": {"prompt": "10", "completion": "2"}},
            {"id": "vendor/unknown", "name": "Unknown", "pricing": {"prompt": None, "completion": None}},
        ]
        self.assertEqual([model["id"] for model in _sort_models(models, {"vendor/favorite"})], [
            "vendor/favorite", "vendor/free", "vendor/unknown",
        ])
        self.assertEqual(models[0]["id"], "vendor/free")
        self.assertEqual([model["id"] for model in _sort_models(models)], [
            "vendor/free", "vendor/favorite", "vendor/unknown",
        ])

    def test_each_group_retains_price_name_and_id_tie_breakers(self) -> None:
        models = [
            {"id": "vendor/z", "name": "Same", "pricing": {"prompt": "1", "completion": "2"}},
            {"id": "vendor/a", "name": "Same", "pricing": {"prompt": "1", "completion": "2"}},
            {"id": "vendor/favorite", "name": "Fav", "pricing": {"prompt": None, "completion": None}},
        ]
        self.assertEqual([model["id"] for model in _sort_models(models, {"vendor/favorite", "vendor/absent"})], [
            "vendor/favorite", "vendor/a", "vendor/z",
        ])


class TelegramZdrTextTests(unittest.IsolatedAsyncioTestCase):
    def test_claude_and_local_models_never_claim_zdr(self) -> None:
        english, spanish = TelegramSession("u", "a"), TelegramSession("u", "a", language="es")
        self.assertEqual(_zdr_line(english, "claude_subscription", True), "Not ZDR (Claude subscription, experimental)")
        self.assertEqual(_zdr_line(spanish, "claude_subscription", True), "Sin ZDR (suscripción de Claude, experimental)")
        self.assertEqual(_zdr_line(english, "ollama", True, "llama3:8b"), "ZDR: not applicable (local Ollama model)")
        self.assertEqual(_zdr_line(spanish, "ollama", True, "ollama:glm-4.7:cloud"), "Sin ZDR (modelo en la nube de Ollama)")
        self.assertEqual(_zdr_line(english, "openrouter", True), "ZDR: enabled")
        self.assertEqual(_zdr_line(spanish, "openrouter", False), "ZDR: desactivado")
        claude = {"id": "claude_subscription:opus", "name": "Claude Opus 5.5 (Claude subscription, experimental)",
                  "pricing": {"prompt": None, "completion": None}, "provider": "claude_subscription"}
        self.assertIn("no ZDR", _llm_label(claude, 1, None, set()))
        self.assertNotIn("$", _llm_label(claude, 1, None, set()))

    def test_selecting_a_claude_model_reports_not_zdr(self) -> None:
        service = Mock()
        service.set_chat_model.return_value = {"provider": "claude_subscription", "id": "claude_subscription:opus", "zdr": False}
        session = TelegramSession("u", "a", "c", language="es")
        panel = _Panel(token="t", chat_id=1, view="chat_llm", agent_id="a", target_chat="c",
                       items=[{"id": "claude_subscription:opus", "name": "Opus"}])
        text = _apply_panel_action(service, session, panel, "pick:0")
        self.assertIn("Sin ZDR (suscripción de Claude, experimental)", text)
        self.assertIn("Claude · claude_subscription:opus", text)
        self.assertNotIn("ZDR: activado", text)

    async def test_current_model_command_reports_not_zdr(self) -> None:
        service = Mock()
        service.get_chat.return_value = {"model": {"provider": "claude_subscription", "id": "claude_subscription:sonnet",
                                                   "zdr": False}, "model_override": {"id": "claude_subscription:sonnet"}}
        state = TelegramBotState(service, {123: TelegramSession("u", "a", "c")})
        reply = AsyncMock()
        update = SimpleNamespace(effective_user=SimpleNamespace(id=123), effective_chat=SimpleNamespace(id=456),
                                 effective_message=SimpleNamespace(reply_text=reply))
        context = SimpleNamespace(application=SimpleNamespace(bot_data={"state": state}), user_data={}, args=[])
        await show_model(update, context)
        self.assertIn("Not ZDR (Claude subscription, experimental)", reply.await_args.args[0])
        self.assertNotIn("ZDR: enabled", reply.await_args.args[0])


class TelegramFavoritePickerTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.service = Mock()
        self.service.list_favorite_models.return_value = ["vendor/11", "vendor/10", "vendor/retired"]
        self.models = [
            {"id": f"vendor/{index}", "name": f"Model {index}", "pricing": {"prompt": str(index), "completion": "0"}}
            for index in range(12)
        ]
        self.service.list_models.return_value = self.models
        self.service.get_chat_models.return_value = {"model": {"id": "vendor/0"}, "model_override": None}
        state = TelegramBotState(self.service, {123: TelegramSession("test-user", "agent-a", "current-chat")})
        self.update = SimpleNamespace(
            effective_user=SimpleNamespace(id=123), effective_chat=SimpleNamespace(id=456),
            effective_message=SimpleNamespace(reply_text=AsyncMock(return_value=SimpleNamespace(message_id=1))),
        )
        self.context = SimpleNamespace(
            bot=Mock(), application=SimpleNamespace(bot_data={"state": state}), user_data={}, args=[],
        )
        self.context.bot.delete_message = AsyncMock()

    async def test_only_favorites_are_offered_and_each_line_names_its_provider(self) -> None:
        await select_model(self.update, self.context)
        ids = [model["id"] for model in self.context.user_data["panel"].items]
        self.assertEqual(ids, ["vendor/10", "vendor/11"])  # no search, no other models, retired favorites skipped
        call = self.update.effective_message.reply_text.await_args
        keyboard = call.kwargs["reply_markup"].inline_keyboard
        self.assertEqual(keyboard[0][0].text, "Use user default")
        for row in keyboard[1:3]:
            self.assertIn("⭐", row[0].text)
            self.assertIn("OpenRouter · ", row[0].text)
        self.assertIn("Page 1/1", call.args[0])
        self.service.list_models.assert_called_once_with("test-user", "", zdr=True)

    async def test_query_arguments_are_ignored(self) -> None:
        self.context.args = ["Model", "0"]
        await select_model(self.update, self.context)
        self.assertEqual([model["id"] for model in self.context.user_data["panel"].items], ["vendor/10", "vendor/11"])
        self.service.list_models.assert_called_once_with("test-user", "", zdr=True)

    async def test_same_model_name_from_two_providers_is_told_apart(self) -> None:
        self.service.list_favorite_models.return_value = ["anthropic/claude-haiku-5.5", "claude_subscription:haiku"]
        self.service.list_models.return_value = [
            {"id": "anthropic/claude-haiku-5.5", "name": "Claude Haiku 5.5", "provider": "openrouter",
             "pricing": {"prompt": "1", "completion": "5"}},
            {"id": "claude_subscription:haiku", "name": "Claude Haiku 5.5", "provider": "claude_subscription",
             "pricing": {"prompt": None, "completion": None}},
        ]
        await select_model(self.update, self.context)
        keyboard = self.update.effective_message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard
        labels = [row[0].text for row in keyboard[1:3]]
        self.assertTrue(any("OpenRouter · Claude Haiku" in label for label in labels))
        self.assertTrue(any("Claude · Claude Haiku" in label and "no ZDR" in label for label in labels))

    async def test_no_favorites_explains_how_to_add_them(self) -> None:
        self.service.list_favorite_models.return_value = []
        await select_model(self.update, self.context)
        self.assertNotIn("panel", self.context.user_data)
        self.assertIn("favorite", self.update.effective_message.reply_text.await_args.args[0])

    async def test_failed_favorites_read_is_reported_instead_of_falling_back_to_price_only(self) -> None:
        self.service.list_favorite_models.side_effect = FavoritesError(
            "Could not read model favorites; check the user's openrouter-favorites.json.",
        )
        with self.assertLogs("recall.telegram", level="WARNING"):
            await select_model(self.update, self.context)
        self.service.list_models.assert_not_called()
        self.assertNotIn("panel", self.context.user_data)
        self.assertIn("openrouter-favorites.json", self.update.effective_message.reply_text.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
