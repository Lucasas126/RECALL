from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from src.agent.agent import Agent
from src.config.models import ModelsConfig, ResolvedAgent
from src.core.sources.conversations import ConversationsSource
from src.providers.provider import LLMProvider


def _tool_round(name: str = "terminal") -> dict:
    return {"response": "Working on it", "tool_calls": [{"id": "c1", "name": name, "arguments": {}}]}


class CheckpointTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.conversations = ConversationsSource(Path(directory.name))
        registry = Mock()
        registry.conversations.return_value = self.conversations
        self.provider = Mock(spec=LLMProvider)
        self.provider.context_window.return_value = 10000
        config = ResolvedAgent(
            user_id="test-user", id="test-agent", name="Test",
            models=ModelsConfig(), provider_name="ollama", api_key="",
            embeddings_provider_name="ollama", embeddings_api_key="",
            tools=False, associative_memory="off", level="admin",
        )
        with (
            patch("src.agent.agent.SourceRegistry.for_agent", return_value=registry),
            patch("src.agent.agent.VectorIndex.for_agent"),
        ):
            self.agent = Agent(config, self.provider)
        self.agent._tool_handlers = {"terminal": lambda **_: "done"}

    def turns(self) -> list[dict]:
        return self.conversations.load(self.agent.chat_id)

    def test_server_stop_after_a_tool_round_keeps_the_work_done(self) -> None:
        self.provider.invoke.side_effect = [_tool_round(), RuntimeError("server stopped")]
        with self.assertRaises(RuntimeError):
            self.agent.invoke("Do it", stream=False)
        turns = self.turns()
        self.assertEqual([t["role"] for t in turns], ["user", "assistant"])
        saved = turns[-1]
        self.assertTrue(saved["checkpoint"])
        self.assertTrue(saved["interrupted"])
        self.assertEqual(saved["content"], "Working on it")
        self.assertEqual(saved["actions"][0]["name"], "terminal")
        self.assertEqual(saved["actions"][0]["result"], "done")

    def test_final_reply_replaces_the_checkpoint(self) -> None:
        self.provider.invoke.side_effect = [_tool_round(), _tool_round(), {"response": "All done"}]
        self.agent.invoke("Do it", stream=False)
        turns = self.turns()
        self.assertEqual([t["role"] for t in turns], ["user", "assistant"])
        self.assertEqual(turns[-1]["content"], "All done")
        self.assertNotIn("checkpoint", turns[-1])
        self.assertNotIn("interrupted", turns[-1])
        self.assertEqual(len(turns[-1]["actions"]), 2)

    def test_checkpoint_is_replaced_not_stacked(self) -> None:
        self.provider.invoke.side_effect = [_tool_round(), _tool_round(), RuntimeError("stop")]
        with self.assertRaises(RuntimeError):
            self.agent.invoke("Do it", stream=False)
        turns = self.turns()
        self.assertEqual([t["role"] for t in turns], ["user", "assistant"])
        self.assertEqual(len(turns[-1]["actions"]), 2)

    def test_next_message_after_a_cut_reply_keeps_both_turns(self) -> None:
        self.provider.invoke.side_effect = [_tool_round(), RuntimeError("stop")]
        with self.assertRaises(RuntimeError):
            self.agent.invoke("Do it", stream=False)
        self.provider.invoke.side_effect = None
        self.provider.invoke.return_value = {"response": "Fine"}
        self.agent.invoke("Did it work?", stream=False)
        turns = self.turns()
        self.assertEqual([t["role"] for t in turns], ["user", "assistant", "user", "assistant"])
        self.assertTrue(turns[1]["checkpoint"])
        self.assertNotIn("checkpoint", turns[3])

    def test_reload_shows_the_model_the_cut_marker(self) -> None:
        self.provider.invoke.side_effect = [_tool_round(), RuntimeError("stop")]
        with self.assertRaises(RuntimeError):
            self.agent.invoke("Do it", stream=False)
        self.agent.messages = self.agent.messages[:1]
        self.agent._load_recent_context()
        last = self.agent.messages[-1]
        self.assertEqual(last["role"], "assistant")
        self.assertIn("server stopped", last["content"])
        self.assertIn("Working on it", last["content"])

    def test_only_a_checkpoint_is_dropped(self) -> None:
        chat = self.agent.chat_id
        self.conversations.append(chat, "assistant", "normal", interrupted=True)
        self.conversations.append(chat, "assistant", "next", drop_checkpoint=True)
        self.assertEqual([t["content"] for t in self.turns()], ["normal", "next"])


if __name__ == "__main__":
    unittest.main()
