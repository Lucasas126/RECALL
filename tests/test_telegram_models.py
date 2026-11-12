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
    def test_claude_and_local_models_never_claim_zdr_when_it_is_required(self) -> None:
        english, spanish = TelegramSession("u", "a"), TelegramSession("u", "a", language="es")

        def line(session, provider, zdr, model_id="x", require_zdr=True):
            return _zdr_line(session, {"provider": provider, "id": model_id, "zdr": zdr, "require_zdr": require_zdr})

        # ZDR is an OpenRouter filter: a Claude subscription model gets no ZDR line at all
        self.assertEqual(line(english, "claude_subscription", False), "")
        self.assertEqual(line(spanish, "claude_subscription", False), "")
        self.assertEqual(line(english, "ollama", True, "llama3:8b"), "ZDR: not applicable (local Ollama model)")
        self.assertEqual(line(spanish, "ollama", True, "ollama:glm-4.7:cloud"), "Sin ZDR (modelo en la nube de Ollama)")
        self.assertEqual(line(english, "openrouter", True), "ZDR: enabled")
        self.assertEqual(line(spanish, "openrouter", False), "ZDR: desactivado")
        claude = {"id": "claude_subscription:opus", "name": "Claude Opus 5.5 (Claude subscription, experimental)",
                  "pricing": {"prompt": None, "completion": None}, "provider": "claude_subscription"}
        self.assertNotIn("ZDR", _llm_label(claude, 1, None, set(), True))
        self.assertNotIn("$", _llm_label(claude, 1, None, set(), True))

    def test_no_zdr_wording_when_it_is_not_required(self) -> None:
        english = TelegramSession("u", "a")
        for provider, model_id in (("claude_subscription", "claude_subscription:opus"), ("ollama", "ollama:glm:cloud"),
                                   ("ollama", "llama3:8b"), ("openrouter", "vendor/model")):
            self.assertEqual(_zdr_line(english, {"provider": provider, "id": model_id, "zdr": False, "require_zdr": False}), "")
        # a chat that opted in still says so
        self.assertEqual(_zdr_line(english, {"provider": "openrouter", "id": "v/m", "zdr": True, "require_zdr": False}),
                         "ZDR: enabled")
        claude = {"id": "claude_subscription:opus", "name": "Claude Opus", "pricing": {}, "provider": "claude_subscription"}
        label = _llm_label(claude, 1, None, set(), False)
        self.assertIn("🧪", label)
        self.assertNotIn("ZDR", label)

    def test_selecting_a_claude_model_under_zdr_adds_no_zdr_line(self) -> None:
        service = Mock()
        service.set_chat_model.return_value = {
            "provider": "claude_subscription", "id": "claude_subscription:opus", "zdr": False, "require_zdr": True,
        }
        session = TelegramSession("u", "a", "c", language="es")
        panel = _Panel(token="t", chat_id=1, view="chat_llm", agent_id="a", target_chat="c",
                       items=[{"id": "claude_subscription:opus", "name": "Opus"}])
        text = _apply_panel_action(service, session, panel, "pick:0")
        self.assertIn("Claude · claude_subscription:opus", text)
        self.assertNotIn("ZDR", text)

    def test_selecting_a_model_without_zdr_adds_no_zdr_line(self) -> None:
        service = Mock()
        service.set_chat_model.return_value = {
            "provider": "claude_subscription", "id": "claude_subscription:opus", "zdr": False, "require_zdr": False,
        }
        session = TelegramSession("u", "a", "c")
        panel = _Panel(token="t", chat_id=1, view="chat_llm", agent_id="a", target_chat="c",
                       items=[{"id": "claude_subscription:opus", "name": "Opus"}])
        text = _apply_panel_action(service, session, panel, "pick:0")
        self.assertEqual(text, "✅ Model selected\n\nClaude · claude_subscription:opus\n\nThis selection applies to the current chat.")
        service.set_chat_model.assert_called_once_with("u", "a", "c", "claude_subscription:opus")

    async def test_current_model_command_without_zdr_has_no_zdr_line(self) -> None:
        service = Mock()
        service.get_chat.return_value = {"model": {"provider": "openrouter", "id": "vendor/model", "zdr": False,
                                                   "require_zdr": False}, "model_override": None}
        state = TelegramBotState(service, {123: TelegramSession("u", "a", "c")})
        reply = AsyncMock()
        update = SimpleNamespace(effective_user=SimpleNamespace(id=123), effective_chat=SimpleNamespace(id=456),
                                 effective_message=SimpleNamespace(reply_text=reply))
        context = SimpleNamespace(application=SimpleNamespace(bot_data={"state": state}), user_data={}, args=[])
        await show_model(update, context)
        self.assertEqual(reply.await_args.args[0], "Current chat model:\nOpenRouter · vendor/model\nUser default")

    async def test_current_model_command_has_no_zdr_line_for_claude(self) -> None:
        service = Mock()
        service.get_chat.return_value = {"model": {"provider": "claude_subscription", "id": "claude_subscription:sonnet",
                                                   "zdr": False, "require_zdr": True},
                                         "model_override": {"id": "claude_subscription:sonnet"}}
        state = TelegramBotState(service, {123: TelegramSession("u", "a", "c")})
        reply = AsyncMock()
        update = SimpleNamespace(effective_user=SimpleNamespace(id=123), effective_chat=SimpleNamespace(id=456),
                                 effective_message=SimpleNamespace(reply_text=reply))
        context = SimpleNamespace(application=SimpleNamespace(bot_data={"state": state}), user_data={}, args=[])
        await show_model(update, context)
        self.assertNotIn("ZDR", reply.await_args.args[0])


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
        self.service.require_zdr.return_value = False
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
        self.assertNotIn("ZDR", call.args[0])
        self.service.list_models.assert_called_once_with("test-user", "")

    async def test_query_arguments_are_ignored(self) -> None:
        self.context.args = ["Model", "0"]
        await select_model(self.update, self.context)
        self.assertEqual([model["id"] for model in self.context.user_data["panel"].items], ["vendor/10", "vendor/11"])
        self.service.list_models.assert_called_once_with("test-user", "")

    async def test_the_picker_mentions_zdr_only_when_the_user_requires_it(self) -> None:
        self.service.require_zdr.return_value = True
        await select_model(self.update, self.context)
        self.assertIn("ZDR enabled", self.update.effective_message.reply_text.await_args.args[0])

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
        self.assertTrue(any("Claude · Claude Haiku" in label and "plan" in label and "ZDR" not in label for label in labels))

    async def test_claude_favorites_carry_no_zdr_wording_when_the_user_requires_it(self) -> None:
        self.service.require_zdr.return_value = True
        self.service.list_favorite_models.return_value = ["claude_subscription:haiku"]
        self.service.list_models.return_value = [
            {"id": "claude_subscription:haiku", "name": "Claude Haiku 5.5", "provider": "claude_subscription",
             "pricing": {"prompt": None, "completion": None}},
        ]
        await select_model(self.update, self.context)
        keyboard = self.update.effective_message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard
        self.assertIn("plan", keyboard[1][0].text)
        self.assertNotIn("ZDR", keyboard[1][0].text)

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
