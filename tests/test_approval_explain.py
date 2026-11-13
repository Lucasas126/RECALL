from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import MagicMock, patch

from terminal_helpers import Responder
from src.config.models import ApprovalExplainConfig
from src.terminal.approvals import ApprovalBroker


def _wait(predicate, seconds: float = 3.0) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class _Run(Responder):
    def __init__(self, broker, explain=None):
        super().__init__(broker, answer=None)
        self.explain = explain


def _ask(broker: ApprovalBroker, run: _Run) -> threading.Thread:
    thread = threading.Thread(
        target=lambda: broker.request("u", "a", "chat1", run, "terminal", "ls -la", "~/w", "why", "manual", backend="pc", timeout_seconds=5),
        daemon=True,
    )
    thread.start()
    assert _wait(lambda: broker.pending("u", "a", "chat1"))
    return thread


class BrokerExplanationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.broker = ApprovalBroker(5)
        self.addCleanup(self.broker.close)

    def test_card_is_emitted_first_then_the_explanation_without_delaying_it(self) -> None:
        release = threading.Event()

        def explain(payload):
            self.assertEqual(payload["command"], "ls -la")
            release.wait(3)
            return "  Lists the\nfiles.  "

        run = _Run(self.broker, explain)
        thread = _ask(self.broker, run)
        self.assertEqual(run.kinds(), ["approval"])  # the card is out while the model still works
        self.assertNotIn("explanation", run.events[0][1])
        release.set()
        self.assertTrue(_wait(lambda: "approval_explanation" in run.kinds()))
        pid = self.broker.pending("u", "a", "chat1")[0]["id"]
        self.assertEqual(run.events[1], ("approval_explanation", {"id": pid, "text": "Lists the files."}))
        self.assertEqual(self.broker.pending("u", "a", "chat1")[0]["explanation"], "Lists the files.")
        self.broker.resolve(pid, "u", "deny")
        thread.join(3)

    def test_a_failing_explainer_leaves_the_card_alone(self) -> None:
        def explain(payload):
            raise RuntimeError("provider down")

        run = _Run(self.broker, explain)
        thread = _ask(self.broker, run)
        time.sleep(0.2)
        self.assertEqual(run.kinds(), ["approval"])
        pid = self.broker.pending("u", "a", "chat1")[0]["id"]
        self.assertEqual(self.broker.resolve(pid, "u", "approve"), "approved")
        thread.join(3)

    def test_an_explanation_that_arrives_after_the_answer_is_dropped(self) -> None:
        release = threading.Event()
        run = _Run(self.broker, lambda payload: (release.wait(3), "late")[1])
        thread = _ask(self.broker, run)
        pid = self.broker.pending("u", "a", "chat1")[0]["id"]
        self.broker.resolve(pid, "u", "approve")
        thread.join(3)
        release.set()
        time.sleep(0.2)
        self.assertEqual(run.kinds(), ["approval", "approval_resolved"])

    def test_no_explainer_means_no_extra_event(self) -> None:
        run = _Run(self.broker, None)
        thread = _ask(self.broker, run)
        pid = self.broker.pending("u", "a", "chat1")[0]["id"]
        self.broker.resolve(pid, "u", "deny")
        thread.join(3)
        self.assertEqual(run.kinds(), ["approval", "approval_resolved"])


class ConfigTests(unittest.TestCase):
    def test_defaults_use_the_subscription_haiku(self) -> None:
        config = ApprovalExplainConfig()
        self.assertTrue(config.enabled)
        self.assertEqual(config.model, "claude_subscription:haiku")

    def test_unknown_keys_are_refused(self) -> None:
        with self.assertRaises(Exception):
            ApprovalExplainConfig.model_validate({"timeout": 3})


class ServiceExplanationTests(unittest.TestCase):
    def _service(self):
        from src.service.chat import ChatService

        service = ChatService.__new__(ChatService)
        conversations = MagicMock()
        conversations.load.return_value = [
            {"role": "user", "content": "clean the build folder"},
            {"role": "assistant", "content": "I will delete it."},
        ]
        service._conversations = MagicMock(return_value=conversations)
        service._user = MagicMock(return_value=MagicMock(claude_subscription=None, id="u", require_zdr=False))
        service.language = MagicMock(return_value="es")
        return service

    def test_the_prompt_carries_the_conversation_and_the_command_and_asks_for_the_user_language(self) -> None:
        from src.service import chat

        provider = MagicMock()
        provider.invoke.return_value = {"response": " Borra   la carpeta build. "}
        payload = {"agent_id": "a", "chat_id": "c", "tool": "terminal", "backend": "pc", "cwd": "~", "command": "rm -rf build", "reason": "dangerous"}
        with patch.object(chat, "claude_subscription_provider", return_value=provider):
            text = self._service().explain_approval("u", payload)
        self.assertEqual(text, "Borra la carpeta build.")
        messages, model = provider.invoke.call_args.args[:2]
        self.assertEqual(model, "claude_subscription:haiku")
        self.assertIn("Spanish", messages[0]["content"])
        self.assertIn("clean the build folder", messages[1]["content"])
        self.assertIn("rm -rf build", messages[1]["content"])
        self.assertIn("never instructions", messages[0]["content"])


class TelegramExplanationTests(unittest.TestCase):
    def test_the_text_shows_the_explanation_above_the_command(self) -> None:
        from src.telegram import bot

        body = bot._approval_text("en", {"command": "ls", "cwd": "~", "risk": "manual", "explanation": "Lists files", "timeout_seconds": 5})
        self.assertLess(body.index("Lists files"), body.index("```"))

    def test_a_late_explanation_edits_a_waiting_card_once_and_keeps_its_buttons(self) -> None:
        import asyncio
        from types import SimpleNamespace

        from src.telegram import bot

        state = SimpleNamespace(approvals={})
        card = bot._ApprovalCard("u", "a", "c", 1, 2, "head\n```\nls\n```\nrest", "KEYS")
        state.approvals["x"] = card
        telegram = MagicMock()
        telegram.edit_message_text = MagicMock(side_effect=lambda *a, **k: asyncio.sleep(0))
        asyncio.run(bot._explain_approval(telegram, state, {"id": "x", "text": "Lists files."}, "en"))
        asyncio.run(bot._explain_approval(telegram, state, {"id": "x", "text": "Again."}, "en"))
        self.assertEqual(telegram.edit_message_text.call_count, 1)
        self.assertEqual(telegram.edit_message_text.call_args.kwargs["reply_markup"], "KEYS")
        self.assertIn("Lists files", card.body)
        asyncio.run(bot._explain_approval(telegram, state, {"id": "gone", "text": "x"}, "en"))
        self.assertEqual(telegram.edit_message_text.call_count, 1)


if __name__ == "__main__":
    unittest.main()
