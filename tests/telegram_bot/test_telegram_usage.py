from __future__ import annotations

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from src.telegram import bot
from src.telegram.bot import TelegramSession, usage_command


def _update():
    message = SimpleNamespace(reply_text=AsyncMock())
    return SimpleNamespace(effective_message=message), message


def _context(service):
    state = SimpleNamespace(service=service)
    return SimpleNamespace(application=SimpleNamespace(bot_data={"state": state}), args=[])


class TelegramUsageCommandTests(unittest.TestCase):
    def test_replies_with_the_service_answer(self) -> None:
        update, message = _update()
        service = Mock()
        service.agent_owner.side_effect = lambda person, agent: person
        service.usage_command.return_value = "Claude plan usage\nSession (5 h): 6%"
        session = TelegramSession(user_id="user-one", agent_id="agent-one")
        with patch.object(bot, "_session", return_value=session):
            asyncio.run(usage_command(update, _context(service)))
        service.usage_command.assert_called_once_with("user-one", "/usage")
        message.reply_text.assert_awaited_once_with("Claude plan usage\nSession (5 h): 6%")

    def test_unknown_telegram_user_is_refused(self) -> None:
        update, message = _update()
        service = Mock()
        service.agent_owner.side_effect = lambda person, agent: person
        with patch.object(bot, "_session", return_value=None), patch.object(bot, "_unauthorized", AsyncMock()) as refused:
            asyncio.run(usage_command(update, _context(service)))
        refused.assert_awaited_once()
        service.usage_command.assert_not_called()

    def test_help_lists_usage_in_both_languages(self) -> None:
        for language, expected in (("en", "/usage — Claude plan usage"), ("es", "/usage — Uso del plan de Claude")):
            update, message = _update()
            session = TelegramSession(user_id="user-one", agent_id="agent-one", language=language)
            with patch.object(bot, "_session", return_value=session):
                asyncio.run(bot.help_command(update, _context(Mock())))
            self.assertIn(expected, message.reply_text.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
