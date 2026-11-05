from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from test_chat_models import ModelFixture
from src.agent.agent import Agent
from src.config.models import ModelsConfig, ResolvedAgent
from src.core.review import render_turn
from src.core.sources.conversations import ConversationsSource
from src.core.timeline import TimelineBuilder
from src.providers.provider import LLMProvider


class TimelineBuilderTests(unittest.TestCase):
    def test_chunks_merge_and_tools_split_segments(self) -> None:
        builder = TimelineBuilder()
        builder.reasoning("a")
        builder.reasoning("b")
        builder.tool()
        builder.reasoning("c\n\n")
        builder.text("Hello ")
        builder.text("world")
        self.assertEqual(builder.build(1), [
            {"type": "reasoning", "text": "ab"}, {"type": "tool", "index": 0},
            {"type": "reasoning", "text": "c"}, {"type": "text", "text": "Hello world"},
        ])

    def test_whitespace_never_opens_a_segment_and_plain_text_has_no_timeline(self) -> None:
        builder = TimelineBuilder()
        builder.text("   ")
        builder.text("Only text")
        self.assertIsNone(builder.build(0))
        builder = TimelineBuilder()
        builder.reasoning("thought")
        builder.text("\n")
        builder.reasoning("more")
        self.assertEqual(builder.build(0), [{"type": "reasoning", "text": "thoughtmore"}])

    def test_actions_that_were_not_announced_are_appended(self) -> None:
        builder = TimelineBuilder()
        builder.reasoning("t")
        self.assertEqual(builder.build(2), [
            {"type": "reasoning", "text": "t"}, {"type": "tool", "index": 0}, {"type": "tool", "index": 1},
        ])


class AgentTimelineTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.conversations = ConversationsSource(Path(directory.name))
        registry = Mock()
        registry.conversations.return_value = self.conversations
        self.provider = Mock(spec=LLMProvider)
        self.provider.context_window.return_value = 10000
        config = ResolvedAgent(
            user_id="test-user", id="test-agent", name="Test", models=ModelsConfig(), provider_name="ollama",
            api_key="", embeddings_provider_name="ollama", embeddings_api_key="", tools=False, associative_memory="off",
        )
        with (
            patch("src.agent.agent.SourceRegistry.for_agent", return_value=registry),
            patch("src.agent.agent.VectorIndex.for_agent"),
        ):
            self.agent = Agent(config, self.provider)
        self.agent._tool_handlers = {"echo": lambda **kwargs: "echoed"}

    def turns(self) -> list[dict]:
        return self.conversations.load(self.agent.chat_id)

    def test_streamed_reasoning_tool_reasoning_text_is_stored_in_order(self) -> None:
        call = {"id": "c1", "name": "echo", "arguments": {"text": "hi"}}
        hops = iter([
            (e for e in [("reasoning", "first "), ("reasoning", "thought"), {"tool_calls": [call]}]),
            (e for e in [("reasoning", "second"), ("content", "Done "), ("content", "now"), {"usage": {"prompt_tokens": 1, "completion_tokens": 1}}]),
        ])
        self.provider.invoke.side_effect = lambda *args, **kwargs: next(hops)
        events = list(self.agent.invoke("Question"))
        self.assertEqual([kind for kind, _ in events], ["reasoning", "reasoning", "tool_start", "tool", "reasoning", "content", "content"])
        stored = self.turns()[-1]
        self.assertEqual(stored["timeline"], [
            {"type": "reasoning", "text": "first thought"}, {"type": "tool", "index": 0},
            {"type": "reasoning", "text": "second"}, {"type": "text", "text": "Done now"},
        ])
        # the old fields are exactly what they were
        self.assertEqual(stored["content"], "Done now")
        self.assertEqual(stored["reasoning"], "second")
        self.assertEqual([action["name"] for action in stored["actions"]], ["echo"])
        self.assertEqual(stored["actions"][0]["arguments"], {"text": "hi"})

    def test_reply_without_thinking_or_tools_has_no_timeline(self) -> None:
        self.provider.invoke.side_effect = lambda *args, **kwargs: (e for e in [("content", "Hi")])
        list(self.agent.invoke("Question"))
        self.assertNotIn("timeline", self.turns()[-1])

    def test_non_streamed_reply_keeps_the_order_too(self) -> None:
        call = {"id": "c1", "name": "echo", "arguments": {}}
        results = iter([
            {"reasoning": "plan", "response": "", "tool_calls": [call]},
            {"reasoning": "wrap up", "response": "Final"},
        ])
        self.provider.invoke.side_effect = lambda *args, **kwargs: next(results)
        self.assertEqual(self.agent.invoke("Question", stream=False), "Final")
        self.assertEqual(self.turns()[-1]["timeline"], [
            {"type": "reasoning", "text": "plan"}, {"type": "tool", "index": 0},
            {"type": "reasoning", "text": "wrap up"}, {"type": "text", "text": "Final"},
        ])


class TimelineReadersTests(ModelFixture):
    def append(self, **extra) -> None:
        self.service._conversations("user-one", "agent-one").append(self.chat, "assistant", "Answer", **extra)

    def test_web_history_returns_the_timeline_and_old_messages_load_unchanged(self) -> None:
        timeline = [{"type": "tool", "index": 0}, {"type": "text", "text": "Answer"}]
        actions = [{"name": "echo", "arguments": {}, "result": "ok", "error": False}]
        self.append(reasoning="old", actions=actions, timeline=timeline)
        self.append(reasoning="older")
        new, old = self.service.get_chat("user-one", "agent-one", self.chat)["messages"]
        self.assertEqual(new["timeline"], timeline)
        self.assertEqual(new["actions"], actions)
        self.assertEqual(old, {"index": 1, "role": "assistant", "content": "Answer", "ts": old["ts"], "reasoning": "older"})

    def test_dream_view_ignores_the_timeline(self) -> None:
        turn = {"role": "assistant", "content": "Answer", "ts": "t", "timeline": [{"type": "text", "text": "Answer"}],
                "actions": [{"name": "echo"}]}
        self.assertEqual(render_turn(3, turn), {"n": 3, "role": "assistant", "ts": "t", "content": "Answer", "tools": ["echo"]})


if __name__ == "__main__":
    unittest.main()
