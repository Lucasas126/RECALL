from __future__ import annotations

import asyncio
import unittest
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock, patch

from src.config.models import OrchestratorConfig
from src.service.chat import RouteDecision
from src.telegram.bot import new_chat, panel_callback, reroute_callback, text_message
import test_telegram_reroute as base

AGENTS = [{"id": "agent-a", "name": "First"}, {"id": "agent-b", "name": "Second"}, {"id": "agent-c", "name": "Third"}]


class TelegramOrchestratorTests(unittest.IsolatedAsyncioTestCase):
    # reuse the fixtures and helpers of the re-route tests without re-running their tests
    setUp_base = base.TelegramRerouteTests.setUp
    ago = staticmethod(base.TelegramRerouteTests.ago)
    markup_buttons = base.TelegramRerouteTests.markup_buttons
    query = base.TelegramRerouteTests.query

    def setUp(self) -> None:
        self.setUp_base()
        self.service.list_agents.return_value = AGENTS
        self.service.orchestrator_active.return_value = True
        self.service.orchestrator_core_agent.return_value = None
        self.service.route_message.return_value = self.decision("agent-b", 0.9)
        self.context.bot.send_chat_action = AsyncMock()
        self.session.last_message_at = datetime.now(timezone.utc) - timedelta(hours=2)
        self.config(grace_seconds=30, uncertain_grace_seconds=30)

    def config(self, **values) -> None:
        patcher = patch("src.telegram.bot.ORCHESTRATOR", OrchestratorConfig(enabled=True, **values))
        patcher.start()
        self.addCleanup(patcher.stop)

    @staticmethod
    def decision(agent: str, confidence: float, ranking=None, *, core=None) -> RouteDecision:
        ranking = ranking or [(agent, confidence)]
        return RouteDecision(
            agent_id=agent, confidence=confidence, ranking=ranking, uncertain=confidence < 0.6, core_agent_id=core,
        )

    async def send(self, text: str = "hello there") -> AsyncMock:
        self.message.text = text
        with patch("src.telegram.bot._send_generation", new=AsyncMock()) as generation:
            await text_message(self.update, self.context)
            await base._drain()
        return generation

    def labels(self) -> list[str]:
        return [button.text for button in self.markup_buttons()]

    # ------------------------------------------------------------------ confident

    async def test_confident_pick_creates_that_agents_chat_and_announces_it_first(self) -> None:
        generation = await self.send()
        generation.assert_not_called()
        self.service.route_message.assert_called_once_with("test-user", "hello there")  # only the text
        self.context.bot.send_chat_action.assert_awaited_once_with(chat_id=456, action="typing")
        self.service.create_chat.assert_called_with("test-user", "agent-b", origin="telegram")
        self.service.discard_chat.assert_called_once_with("test-user", "agent-a", "chat-of-agent-a")
        self.assertEqual((self.session.agent_id, self.session.chat_id), ("agent-b", "chat-of-agent-b"))
        self.assertEqual(self.preferences["agent_id"], "agent-a")  # an automatic pick keeps the preference
        call = self.message.reply_text.await_args
        self.assertEqual(call.args[0], "It has been a while. I chose Second for this message.")
        self.assertEqual([e.type for e in call.kwargs["entities"]], ["bold"])
        self.assertEqual(self.labels(), ["↩ Continue previous chat", "🔀 Other agent"])
        for button in self.markup_buttons():
            self.assertLessEqual(len(button.callback_data.encode()), 64)

    async def test_confident_pick_answers_after_the_grace_period(self) -> None:
        self.config(grace_seconds=0.05)
        with patch("src.telegram.bot._send_generation", new=AsyncMock()) as generation:
            self.message.text = "hello there"
            await text_message(self.update, self.context)
            generation.assert_not_called()
            await asyncio.sleep(0.3)
        generation.assert_called_once()
        self.assertEqual(generation.await_args.args[2:4], ("hello there", []))
        self.assertEqual(self.session.chat_id, "chat-of-agent-b")
        self.assertIsNone(self.session.pending_reroute)
        self.service.discard_chat.assert_called_once()  # only the empty default-agent chat

    async def test_pick_equal_to_the_default_agent_keeps_the_chat(self) -> None:
        self.service.route_message.return_value = self.decision("agent-a", 0.95)
        await self.send()
        self.service.discard_chat.assert_not_called()
        self.assertEqual(self.session.chat_id, "chat-of-agent-a")
        self.assertIn("I chose First", self.message.reply_text.await_args.args[0])

    async def test_continue_previous_still_works_after_a_pick(self) -> None:
        await self.send()
        query, update = self.query(self.markup_buttons()[0].callback_data, 55)
        with patch("src.telegram.bot._send_generation", new=AsyncMock()) as generation:
            await reroute_callback(update, self.context)
            await base._drain()
        self.assertEqual((self.session.agent_id, self.session.chat_id), ("agent-a", "old-chat"))
        self.service.discard_chat.assert_called_with("test-user", "agent-b", "chat-of-agent-b")
        self.assertEqual(generation.await_args.args[2:4], ("hello there", []))

    async def test_other_agent_opens_the_full_picker_and_replays(self) -> None:
        await self.send()
        query, update = self.query(self.markup_buttons()[1].callback_data, 55)
        await reroute_callback(update, self.context)
        buttons = [b for row in query.message.reply_text.await_args.kwargs["reply_markup"].inline_keyboard for b in row]
        self.assertEqual([b.text for b in buttons][-1], "✖ Cancel (keep current chat)")
        self.assertEqual(len(buttons), 4)  # three agents and cancel
        self.context.user_data["panel"].message_id = 55
        picked, update = self.query(buttons[2].callback_data, 55)
        with patch("src.telegram.bot._send_generation", new=AsyncMock()) as generation:
            await panel_callback(update, self.context)
            await base._drain()
        self.assertEqual(self.session.chat_id, "chat-of-agent-c")
        self.assertEqual(generation.await_args.args[2:4], ("hello there", []))

    # ------------------------------------------------------------------ uncertain

    def uncertain(self, *, core: str | None) -> None:
        self.service.orchestrator_core_agent.return_value = core
        self.service.route_message.return_value = self.decision(
            "agent-b", 0.4, [("agent-b", 0.4), ("agent-c", 0.3), ("agent-a", 0.2)], core=core,
        )

    async def test_uncertain_defaults_to_the_core_agent_and_offers_the_top_candidates(self) -> None:
        self.uncertain(core="agent-c")
        generation = await self.send("hi")
        generation.assert_not_called()
        self.assertEqual(self.session.chat_id, "chat-of-agent-c")
        self.assertEqual(
            self.labels(),
            ["Second", "⭐ Third", "First", "↩ Continue previous chat", "🔀 More agents"],
        )
        self.assertIn("Third will answer in 30 seconds", self.message.reply_text.await_args.args[0])
        for button in self.markup_buttons():
            self.assertLessEqual(len(button.callback_data.encode()), 64)

    async def test_uncertain_without_a_core_agent_defaults_to_the_default_agent(self) -> None:
        self.uncertain(core=None)
        await self.send("hi")
        self.assertEqual(self.session.chat_id, "chat-of-agent-a")
        self.assertIn("⭐ First", self.labels())
        self.service.create_chat.assert_called_once()  # the auto chat of the default agent is kept

    async def test_uncertain_without_a_core_agent_uses_the_default_not_the_last_used_agent(self) -> None:
        self.preferences["default_agent_id"] = "agent-c"  # the last used agent stays agent-a
        self.uncertain(core=None)
        await self.send("hi")
        self.assertEqual(self.session.chat_id, "chat-of-agent-c")
        self.assertIn("⭐ Third", self.labels())
        self.service.create_chat.assert_called_once_with("test-user", "agent-c", origin="telegram")

    async def test_tap_on_a_candidate_replays_in_a_new_chat_of_that_agent(self) -> None:
        self.uncertain(core="agent-c")
        await self.send("hi")
        query, update = self.query(self.markup_buttons()[0].callback_data, 55)
        with patch("src.telegram.bot._send_generation", new=AsyncMock()) as generation:
            await reroute_callback(update, self.context)
            await base._drain()
        self.service.create_chat.assert_called_with("test-user", "agent-b", origin="telegram")
        self.service.discard_chat.assert_called_with("test-user", "agent-c", "chat-of-agent-c")
        self.assertEqual((self.session.agent_id, self.session.chat_id), ("agent-b", "chat-of-agent-b"))
        self.assertEqual(generation.await_args.args[2:4], ("hi", []))
        self.assertIsNone(self.session.pending_reroute)

    async def test_tap_on_the_default_candidate_answers_at_once(self) -> None:
        self.uncertain(core="agent-c")
        await self.send("hi")
        query, update = self.query(self.markup_buttons()[1].callback_data, 55)
        with patch("src.telegram.bot._send_generation", new=AsyncMock()) as generation:
            await reroute_callback(update, self.context)
            await base._drain()
        generation.assert_called_once()
        self.assertEqual(self.session.chat_id, "chat-of-agent-c")
        self.assertIsNone(self.session.pending_reroute)

    async def test_no_tap_uses_the_default_after_the_uncertain_grace(self) -> None:
        self.uncertain(core="agent-c")
        self.config(uncertain_grace_seconds=0.05, grace_seconds=30)
        with patch("src.telegram.bot._send_generation", new=AsyncMock()) as generation:
            self.message.text = "hi"
            await text_message(self.update, self.context)
            await asyncio.sleep(0.3)
        generation.assert_called_once()
        self.assertEqual(self.session.chat_id, "chat-of-agent-c")

    async def test_unknown_candidate_index_is_refused(self) -> None:
        self.uncertain(core="agent-c")
        await self.send("hi")
        record = self.session.pending_reroute
        query, update = self.query(f"r:{record.token}:s9", 55)
        await reroute_callback(update, self.context)
        self.assertIsNotNone(self.session.pending_reroute)

    # ------------------------------------------------------------------ first message

    async def test_first_ever_message_is_routed_without_a_continue_button(self) -> None:
        self.session.chat_id, self.session.last_message_at = None, None
        self.service.latest_chat.return_value = None
        await self.send()
        self.assertEqual(self.labels(), ["🔀 Other agent"])
        self.assertEqual(self.message.reply_text.await_args.args[0], "I chose Second for this message.")
        self.assertEqual(self.session.chat_id, "chat-of-agent-b")

    async def test_first_ever_message_without_a_router_answer_has_no_buttons(self) -> None:
        self.session.chat_id, self.session.last_message_at = None, None
        self.service.latest_chat.return_value = None
        self.service.route_message.return_value = None
        generation = await self.send()
        generation.assert_called_once()
        self.message.reply_text.assert_not_awaited()
        self.assertIsNone(self.session.pending_reroute)

    # ------------------------------------------------------------------ never routed / fallbacks

    async def test_resume_never_calls_the_router(self) -> None:
        self.session.last_message_at = datetime.now(timezone.utc)
        await self.send()
        self.service.route_message.assert_not_called()
        self.assertEqual(self.session.chat_id, "old-chat")

    async def test_explicit_newchat_and_selectagent_never_call_the_router(self) -> None:
        await new_chat(self.update, self.context)
        self.context.user_data["panel"].message_id = 55
        query, update = self.query(self.markup_buttons()[1].callback_data, 55)
        await panel_callback(update, self.context)
        self.service.route_message.assert_not_called()
        generation = await self.send()  # the chat is fresh: the next message is not routed either
        self.service.route_message.assert_not_called()
        generation.assert_called_once()

    async def test_inactive_orchestrator_keeps_the_old_flow(self) -> None:
        self.service.orchestrator_active.return_value = False
        await self.send()
        self.service.route_message.assert_not_called()
        self.context.bot.send_chat_action.assert_not_awaited()
        self.assertIn("Starting a new chat with First", self.message.reply_text.await_args.args[0])
        self.assertEqual(self.labels(), ["↩ Continue previous chat", "🔀 Change agent"])

    async def test_router_failures_fall_back_to_the_default_agent(self) -> None:
        self.service.route_message.return_value = None
        await self.send()
        self.assertIn("Starting a new chat with First", self.message.reply_text.await_args.args[0])
        self.assertEqual(self.session.chat_id, "chat-of-agent-a")
        self.service.discard_chat.assert_not_called()

    async def test_router_exception_and_unknown_agent_fall_back(self) -> None:
        for outcome in (RuntimeError("boom"), self.decision("ghost", 0.99)):
            with self.subTest(outcome=outcome):
                self.setUp()
                self.service.route_message.side_effect = None
                if isinstance(outcome, Exception):
                    self.service.route_message.side_effect = outcome
                else:
                    self.service.route_message.return_value = outcome
                await self.send()
                self.assertEqual(self.session.chat_id, "chat-of-agent-a")
                self.assertIn("Starting a new chat with First", self.message.reply_text.await_args.args[0])

    async def test_a_slow_router_times_out_silently(self) -> None:
        import time
        patcher = patch("src.telegram.bot.ORCHESTRATOR", OrchestratorConfig.model_construct(
            enabled=True, timeout_seconds=0.01, grace_seconds=30, uncertain_grace_seconds=30,
        ))
        patcher.start()
        self.addCleanup(patcher.stop)
        self.service.route_message.side_effect = lambda *args: time.sleep(0.8)
        await self.send()
        self.assertIn("Starting a new chat with First", self.message.reply_text.await_args.args[0])
        self.assertEqual(self.session.chat_id, "chat-of-agent-a")

    async def test_message_without_text_uses_the_core_agent_without_calling_the_router(self) -> None:
        self.service.orchestrator_core_agent.return_value = "agent-b"
        await self.send("")
        self.service.route_message.assert_not_called()
        self.assertEqual(self.session.chat_id, "chat-of-agent-b")
        self.assertIn("Starting a new chat with Second", self.message.reply_text.await_args.args[0])


if __name__ == "__main__":
    unittest.main()
