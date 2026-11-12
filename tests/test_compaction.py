from __future__ import annotations

import copy
import re
import unittest

from pydantic import ValidationError

import test_service_steering as steering
from src.agent import compaction
from src.agent.agent import Agent
from src.config.models import CompactionConfig, ModelsConfig, ResolvedAgent


def _conversation(pairs: int, size: int) -> list[dict]:
    messages = [{"role": "system", "content": "persona"}]
    for i in range(pairs):
        messages.append({"role": "user", "content": f"question {i} " + "x" * size})
        messages.append({"role": "assistant", "content": f"answer {i} " + "y" * size})
    return messages


class CompactionConfigTests(unittest.TestCase):
    def test_defaults_are_percent_of_the_window_with_the_subscription_haiku(self) -> None:
        config = CompactionConfig()
        self.assertEqual((config.mode, config.trigger, config.keep_recent), ("percent", 50, 20))
        self.assertEqual(config.model, "claude_subscription:haiku")

    def test_bounds(self) -> None:
        for bad in (
            {"trigger": 96}, {"keep_recent": 50, "trigger": 40},
            {"mode": "tokens", "trigger": 500, "keep_recent": 100}, {"model": "claude_subscription:"},
        ):
            with self.subTest(config=bad), self.assertRaises(ValidationError):
                CompactionConfig.model_validate(bad)
        CompactionConfig.model_validate({"mode": "tokens", "trigger": 150_000, "keep_recent": 30_000, "model": None})

    def test_thresholds_per_mode(self) -> None:
        self.assertEqual(compaction.thresholds(CompactionConfig(), 200_000), (100_000, 40_000))
        self.assertEqual(compaction.thresholds(CompactionConfig(), 0), (None, 0))
        tokens = CompactionConfig(mode="tokens", trigger=150_000, keep_recent=30_000)
        self.assertEqual(compaction.thresholds(tokens, 0), (150_000, 30_000))
        self.assertEqual(compaction.thresholds(tokens, 100_000), (90_000, 30_000))  # never past the hard limit


class SplitTests(unittest.TestCase):
    def test_tail_opens_on_a_user_message_and_keeps_the_latest_one(self) -> None:
        messages = _conversation(10, 4_000)
        cut = compaction.split(messages, total_tokens=20_000, keep_tokens=4_000)
        self.assertIsNotNone(cut)
        self.assertEqual(messages[cut.start]["role"], "user")
        self.assertLess(cut.start, len(messages) - 1)
        self.assertGreater(cut.summarised_tokens, cut.tail_tokens)

    def test_never_starts_on_a_tool_result(self) -> None:
        messages = _conversation(6, 4_000)
        messages[7] = {"role": "assistant", "content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "read", "arguments": "{}"}}]}
        messages.insert(8, {"role": "tool", "tool_call_id": "c1", "name": "read", "content": "z" * 4_000})
        for keep in range(1_000, 12_000, 500):
            cut = compaction.split(messages, 15_000, keep)
            if cut is not None:
                self.assertEqual(messages[cut.start]["role"], "user")

    def test_too_little_to_summarise_is_skipped_unless_forced(self) -> None:
        messages = _conversation(2, 100)
        self.assertIsNone(compaction.split(messages, 200, 50))
        self.assertIsNotNone(compaction.split(messages, 200, 50, force=True))

    def test_a_single_exchange_cannot_be_split(self) -> None:
        self.assertIsNone(compaction.split(_conversation(1, 10_000), 5_000, 0, force=True))


class TranscriptTests(unittest.TestCase):
    def test_previous_notes_tool_calls_and_instructions_are_included(self) -> None:
        text = compaction.transcript([
            {"role": "user", "content": "deploy it"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "1", "function": {"name": "terminal", "arguments": '{"command": "make"}'}}]},
            {"role": "tool", "name": "terminal", "content": "r" * 5_000},
        ], "earlier facts", "keep the version numbers")
        self.assertIn("<earlier_notes>\nearlier facts", text)
        self.assertIn('ME (tool call) terminal({"command": "make"})', text)
        self.assertIn("more chars]", text)  # long tool results are cut
        self.assertIn("keep the version numbers", text)

    def test_summary_message_tells_the_agent_where_the_full_history_is(self) -> None:
        message = compaction.summary_message("I did things.")
        self.assertEqual(message["role"], "system")
        self.assertIn("session_search", message["content"])
        self.assertTrue(message["content"].endswith("I did things."))


class StoredCompactionTests(unittest.TestCase):
    def test_latest_valid_compaction_and_its_boundary(self) -> None:
        history = [
            {"role": "user", "content": "a"}, {"role": "assistant", "content": "b"},
            {"role": "compaction", "content": "s1", "covers_until": 0},
            {"role": "user", "content": "c"}, {"role": "assistant", "content": "d"},
            {"role": "compaction", "content": "s2", "covers_until": 3},
            {"role": "compaction", "content": "bad", "covers_until": 99},
        ]
        self.assertEqual(compaction.last_compaction(history), (5, history[5]))
        self.assertEqual(compaction.covers_until(history, 1), 3)
        self.assertEqual(compaction.covers_until(history, 2), 0)


class CompactionServiceTests(steering.SteeringFixture):
    """A chat that grows past the trigger is compacted after the reply, and reloads from the summary."""

    window = 40_000
    summaries: list[list[dict]]

    def setUp(self) -> None:
        self.summaries = []
        self.compaction = CompactionConfig(model=None)
        super().setUp()
        for provider in self.providers.values():
            provider.context_window.return_value = self.window

    def _factory(self, user: str, agent: str, chat_id: str | None) -> Agent:
        config = ResolvedAgent(
            user_id=user, id=agent, name="Test",
            models=ModelsConfig(), provider_name="ollama", api_key="",
            embeddings_provider_name="ollama", embeddings_api_key="",
            tools=False, associative_memory=False, has_memory=True, level="admin",
            compaction=self.compaction,
        )
        instance = Agent(config, self.providers[(user, agent)], chat_id)
        self.agents[(user, agent)] = instance
        return instance

    def _responder(self, key: tuple[str, str]):
        def respond(messages, model, *, stream=True, cancellation=None, **kwargs):
            if not stream:  # the summary call
                self.summaries.append(copy.deepcopy(messages))
                return {"response": f"I summarised part {len(self.summaries)}.", "usage": None}
            return self._stream(key, messages)
        return respond

    def _stream(self, key, messages):
        snapshot = copy.deepcopy(messages)
        self.calls[key].append({"context": snapshot, "cancellation": None})
        latest = next(m["content"] for m in reversed(snapshot) if m["role"] == "user")
        latest = re.sub(r"^\[[^\]]*\] ?", "", latest)
        yield "content", "Answer " + latest[:20] + " " + "a" * 6_000
        chars = sum(len(m["content"]) for m in snapshot if isinstance(m.get("content"), str))
        yield {"usage": {"prompt_tokens": chars // 4, "completion_tokens": 1_500}}

    def _say(self, text: str) -> None:
        list(self._send(text + " " + "q" * 6_000))
        self._finish_workers()

    def _stored(self) -> list[dict]:
        return self.sources[("user-one", "agent-one")].load(self.chat)

    def test_compacts_past_the_trigger_and_reloads_from_the_summary(self) -> None:
        for i in range(8):
            self._say(f"m{i}")
        stored = self._stored()
        compactions = [t for t in stored if t["role"] == "compaction"]
        self.assertTrue(compactions, "no compaction was recorded")
        last = compactions[-1]
        self.assertLess(last["tokens_after"], last["tokens_before"])
        self.assertEqual(stored[last["covers_until"]]["role"], "user")
        # the next model call sees the system prompt, then my notes as a system message, then the tail
        self._say("after")
        context = self._context()
        self.assertEqual(context[0]["role"], "system")
        self.assertEqual(context[1]["role"], "system")
        self.assertIn("[CONTEXT COMPACTED]", context[1]["content"])
        self.assertIn("I summarised part", context[1]["content"])
        self.assertEqual(context[2]["role"], "user")
        self.assertNotIn("[CUTOFF CONVERSATION]", [m["content"] for m in context])
        # a reload (new process) rebuilds the same context from the jsonl
        agent = self._factory("user-one", "agent-one", self.chat)
        self.assertEqual(agent._summary, self.agents[("user-one", "agent-one")]._summary)
        self.assertEqual(agent.messages[1]["role"], "user")

    def test_the_previous_summary_goes_into_the_next_one(self) -> None:
        for i in range(14):
            self._say(f"m{i}")
        self.assertGreaterEqual(len(self.summaries), 2)
        self.assertIn("<earlier_notes>\nI summarised part 1.", self.summaries[1][1]["content"])

    def test_disabled_compaction_never_summarises(self) -> None:
        self.compaction = CompactionConfig(enabled=False, model=None)
        for i in range(6):
            self._say(f"m{i}")
        self.assertEqual(self.summaries, [])
        self.assertFalse([t for t in self._stored() if t["role"] == "compaction"])

    def test_history_endpoint_shows_the_compaction_divider(self) -> None:
        for i in range(8):
            self._say(f"m{i}")
        messages = self.service.get_chat("user-one", "agent-one", self.chat)["messages"]
        dividers = [m for m in messages if m["role"] == "compaction"]
        self.assertTrue(dividers)
        self.assertIn("tokens_before", dividers[0])
        self.assertIn("tokens_after", dividers[0])

    def test_a_failing_summary_model_keeps_the_chat_working(self) -> None:
        def broken(messages, model, *, stream=True, cancellation=None, **kwargs):
            if not stream:
                raise RuntimeError("summary model down")
            return self._stream(("user-one", "agent-one"), messages)
        self.providers[("user-one", "agent-one")].invoke.side_effect = broken
        for i in range(18):
            self._say(f"m{i}")
        self.assertFalse([t for t in self._stored() if t["role"] == "compaction"])
        tokens = sum(len(m["content"]) for m in self._context() if isinstance(m.get("content"), str)) // 4
        self.assertLess(tokens, self.window)  # the safety net cut the oldest messages
        self.assertIn("[CUTOFF CONVERSATION]", [m["content"] for m in self._context()])


if __name__ == "__main__":
    unittest.main()
