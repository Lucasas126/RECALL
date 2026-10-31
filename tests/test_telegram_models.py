from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from src.config.favorites import FavoritesError
from src.telegram.bot import TelegramBotState, TelegramSession, _sort_models, select_model


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

    async def test_favorites_are_first_marked_and_counted_in_normal_pagination(self) -> None:
        await select_model(self.update, self.context)
        ids = [model["id"] for model in self.context.user_data["panel"].items]
        self.assertEqual(ids[:2], ["vendor/10", "vendor/11"])
        self.assertNotIn("vendor/retired", ids)
        self.assertEqual(len(ids), 12)
        call = self.update.effective_message.reply_text.await_args
        keyboard = call.kwargs["reply_markup"].inline_keyboard
        self.assertEqual(keyboard[0][0].text, "Use user default")
        self.assertIn("⭐", keyboard[1][0].text)
        self.assertIn("⭐", keyboard[2][0].text)
        self.assertNotIn("⭐", keyboard[3][0].text)
        self.assertIn("Page 1/2", call.args[0])
        self.service.list_models.assert_called_once_with("test-user", "", zdr=True)
        self.service.list_favorite_models.assert_called_once_with("test-user")

    async def test_search_does_not_reintroduce_favorites_absent_from_results(self) -> None:
        self.context.args = ["Model", "0"]
        self.service.list_models.return_value = [self.models[0]]
        await select_model(self.update, self.context)
        self.assertEqual([model["id"] for model in self.context.user_data["panel"].items], ["vendor/0"])
        self.service.list_models.assert_called_once_with("test-user", "Model 0", zdr=True)

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
