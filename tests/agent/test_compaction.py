from __future__ import annotations

import copy
import re
import unittest

from pydantic import ValidationError

from tests.tasks import test_service_steering as steering
from src.agent import compaction
from src.agent.agent import Agent
from src.config.models import CompactionConfig, ModelsConfig, ResolvedAgent
from src.utils.i18n import InputError


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


class CompactArgumentTests(unittest.TestCase):
    """`/compact [percent] [focus]`: a leading whole number is the share to keep, the rest is the focus."""

    def test_parsing(self) -> None:
        cases = {
            "": (None, ""), "keep the figures": (None, "keep the figures"), "10": (10, ""), "10%": (10, ""),
            "10 %": (10, ""), "10 keep the figures": (10, "keep the figures"), "  25%  focus here ": (25, "focus here"),
            "1": (1, ""), "90": (90, ""), "10 20 30": (10, "20 30"), "keep 10 things": (None, "keep 10 things"),
            "10x": (None, "10x"), "10.5": (None, "10.5"), "2026 plan": None, "-5": (None, "-5"),
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                if expected is None:
                    with self.assertRaises(InputError):  # 2026 is a number out of range, not a focus
                        compaction.parse_compact_args(text)
                else:
                    self.assertEqual(compaction.parse_compact_args(text), expected)

    def test_out_of_range_is_an_error_that_names_the_range(self) -> None:
        for text in ("0", "91", "100", "0%", "500 keep this"):
            with self.subTest(text=text), self.assertRaises(InputError) as caught:
                compaction.parse_compact_args(text)
            self.assertEqual((caught.exception.values["low"], caught.exception.values["high"]), (1, 90))


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
            tools=False, associative_memory="off", has_memory=True, level="admin",
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

    def test_manual_compact_keeps_the_latest_20_percent_of_the_conversation_by_default(self) -> None:
        for provider in self.providers.values():
            provider.context_window.return_value = 1_000_000  # far from any trigger: only the share counts
        for i in range(10):
            self._say(f"m{i}")
        self.assertEqual(self.summaries, [])
        agent = self.agents[("user-one", "agent-one")]
        result = agent.compact(manual=True)
        self.assertIsNotNone(result)
        self.assertEqual(len(self.summaries), 1)
        self.assertLess(result.tokens_after, result.tokens_before)
        self.assertEqual(agent.messages[1]["role"], "user")
        self.assertIn("m9", agent.messages[-2]["content"])  # the latest exchange stays
        self.assertLessEqual(abs(result.kept_percent - 20), 10)  # whole exchanges, so about 20
        self.assertEqual([t for t in self._stored() if t["role"] == "compaction"][-1]["kept_percent"], result.kept_percent)

    def test_the_share_is_an_argument_and_a_smaller_one_keeps_less(self) -> None:
        for provider in self.providers.values():
            provider.context_window.return_value = 1_000_000
        for i in range(20):
            self._say(f"m{i}")
        agent = self.agents[("user-one", "agent-one")]
        ten = agent.compact(manual=True, keep_percent=10)
        self.assertLessEqual(ten.kept_percent, 15)
        self.assertGreaterEqual(ten.kept_percent, 5)
        for i in range(20, 40):
            self._say(f"m{i}")
        fifty = agent.compact(manual=True, keep_percent=50)
        self.assertGreater(fifty.kept_percent, ten.kept_percent + 20)

    def test_the_system_prompt_is_never_summarised(self) -> None:
        for provider in self.providers.values():
            provider.context_window.return_value = 1_000_000
        for i in range(6):
            self._say(f"m{i}")
        agent = self.agents[("user-one", "agent-one")]
        system = copy.deepcopy(agent.messages[0])
        agent.compact(manual=True, keep_percent=10)
        self.assertEqual(agent.messages[0], system)  # SOUL, memories, skills index, notices: byte for byte
        self.assertNotIn(system["content"], self.summaries[-1][1]["content"])  # nor given to the summary model
        context = agent._context()
        self.assertEqual(context[0], system)
        self.assertIn("[CONTEXT COMPACTED]", context[1]["content"])

    def test_the_share_counts_the_conversation_not_the_fixed_part(self) -> None:
        messages = [{"role": "system", "content": "s" * 80_000}]  # a large fixed prompt
        for i in range(10):
            messages += [{"role": "user", "content": "u" * 2_000}, {"role": "assistant", "content": "a" * 2_000}]
        for share in (0.1, 0.2, 0.5):
            cut = compaction.split(messages, 40_000, 0, force=True, keep_share=share, extra_chars=10_000)
            self.assertLessEqual(abs(cut.kept_percent - share * 100), 10, share)
            self.assertEqual(messages[cut.start]["role"], "user")

    def test_a_tool_round_is_never_split_from_its_user_message(self) -> None:
        messages = [{"role": "system", "content": "p"}]
        for i in range(8):
            messages.append({"role": "user", "content": f"q{i} " + "x" * 1_000})
            for j in range(3):
                messages.append({"role": "assistant", "content": "", "tool_calls": [{"id": f"{i}{j}", "function": {"name": "terminal", "arguments": "{}"}}]})
                messages.append({"role": "tool", "tool_call_id": f"{i}{j}", "name": "terminal", "content": "r" * 1_500})
            messages.append({"role": "assistant", "content": "done " + "y" * 500})
        for share in (0.05, 0.1, 0.2, 0.35, 0.6):
            cut = compaction.split(messages, 30_000, 0, force=True, keep_share=share)
            self.assertEqual(messages[cut.start]["role"], "user", share)
            self.assertIn(messages[cut.start - 1]["role"], ("assistant",), share)  # the part above ends after a whole answer

    def test_manual_compact_does_not_depend_on_the_window_or_the_trigger(self) -> None:
        for window in (40_000, 1_000_000):
            with self.subTest(window=window):
                for provider in self.providers.values():
                    provider.context_window.return_value = window
                self.compaction = CompactionConfig(model=None, mode="tokens", trigger=10_000_000, keep_recent=0)
                self.summaries.clear()
                for i in range(6):
                    self._say(f"w{window}-{i}")
                agent = self.agents[("user-one", "agent-one")]
                result = agent.compact(manual=True)
                self.assertIsNotNone(result)
                self.assertLessEqual(abs(result.kept_percent - 20), 12)

    def test_automatic_compaction_still_keeps_keep_recent_of_the_window(self) -> None:
        cut = compaction.split(_conversation(10, 4_000), 20_000, 4_000)
        self.assertGreater(cut.summarised_tokens, cut.tail_tokens)  # the window-sized tail, as before
        self.assertIsNone(compaction.split(_conversation(2, 100), 200, 50))  # too little to summarise: skipped

    def test_compact_start_returns_before_the_outcome_and_matches_compact_command(self) -> None:
        for provider in self.providers.values():
            provider.context_window.return_value = 1_000_000
        for i in range(12):
            self._say(f"m{i}")
        self.assertIsNone(self.service.compact_start("user-one", "agent-one", self.chat, "hello"))
        events = self.service.compact_start("user-one", "agent-one", self.chat, "/compact 10")
        text = "".join(event.text or "" for event in events if event.kind == "text")
        self.assertIn("Context compacted", text)

    def test_compact_command_takes_a_share_and_a_focus(self) -> None:
        for provider in self.providers.values():
            provider.context_window.return_value = 1_000_000
        for i in range(12):
            self._say(f"m{i}")
        reply = self.service.compact_command("user-one", "agent-one", self.chat, "/compact 10 keep the numbers")
        self.assertIn("Context compacted", reply)
        self.assertRegex(reply, r"The latest \d+ % of the conversation stays word for word")
        self.assertIn("keep the numbers", self.summaries[-1][1]["content"])
        self.assertNotIn("10 keep", self.summaries[-1][1]["content"])  # the share is not part of the focus
        kept = [t for t in self._stored() if t["role"] == "compaction"][-1]["kept_percent"]
        self.assertLessEqual(kept, 16)

    def test_a_share_out_of_range_is_refused_not_read_as_a_focus(self) -> None:
        for text in ("/compact 0", "/compact 95", "/compact 100 %", "/compact 0%"):
            with self.subTest(text=text), self.assertRaises(Exception) as caught:
                self.service.compact_command("user-one", "agent-one", self.chat, text)
            self.assertIn("between 1 and 90", str(caught.exception))
        self.assertEqual(self.summaries, [])

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

    def test_history_endpoint_carries_the_share_kept_by_a_manual_compact(self) -> None:
        for provider in self.providers.values():
            provider.context_window.return_value = 1_000_000
        for i in range(10):
            self._say(f"m{i}")
        self.service.compact_command("user-one", "agent-one", self.chat, "/compact 10")
        divider = [m for m in self.service.get_chat("user-one", "agent-one", self.chat)["messages"] if m["role"] == "compaction"][-1]
        self.assertTrue(divider["manual"])
        self.assertIsInstance(divider["kept_percent"], int)
        self.assertLessEqual(divider["kept_percent"], 16)
        self.assertNotIn("small", divider)

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

    def test_compact_command_summarises_now_with_the_focus(self) -> None:
        self.compaction = CompactionConfig(model=None, mode="tokens", trigger=10_000_000, keep_recent=0)  # never automatic
        for i in range(3):
            self._say(f"m{i}")
        self.assertEqual(self.summaries, [])
        reply = self.service.compact_command("user-one", "agent-one", self.chat, "/compact keep the numbers")
        self.assertRegex(reply, r"^Context compacted: [\d.]+ → [\d.]+ tokens\. The latest \d+ % of the conversation stays word for word\.( More than the 20 % asked.*)?$")
        self.assertEqual(len(self.summaries), 1)
        self.assertIn("keep the numbers", self.summaries[0][1]["content"])
        last = [t for t in self._stored() if t["role"] == "compaction"][-1]
        self.assertTrue(last.get("manual"))
        self.assertFalse([t for t in self._stored() if "/compact" in str(t.get("content"))])  # never stored as a turn
        self._say("after")
        self.assertIn("[CONTEXT COMPACTED]", self._context()[1]["content"])

    def test_compact_command_on_a_short_chat_does_nothing(self) -> None:
        self._say("only")
        reply = self.service.compact_command("user-one", "agent-one", self.chat, "/compact")
        self.assertEqual(reply, "Nothing to compact yet: the chat is still short.")
        self.assertEqual(self.summaries, [])

    def test_other_text_is_not_the_compact_command(self) -> None:
        self.assertIsNone(self.service.compact_command("user-one", "agent-one", self.chat, "/compacting things"))
        self.assertIsNone(self.service.compact_command("user-one", "agent-one", self.chat, "compact please"))

    def test_a_message_sent_during_compact_waits_and_does_not_cancel_it(self) -> None:
        import threading
        self.compaction = CompactionConfig(model=None, mode="tokens", trigger=10_000_000, keep_recent=0)
        for i in range(3):
            self._say(f"m{i}")
        started, release = threading.Event(), threading.Event()
        def slow(messages, model, *, stream=True, cancellation=None, **kwargs):
            if not stream:
                started.set()
                release.wait(5)
                self.summaries.append(copy.deepcopy(messages))
                return {"response": "slow summary", "usage": None}
            return self._stream(("user-one", "agent-one"), messages)
        self.providers[("user-one", "agent-one")].invoke.side_effect = slow
        result: list[str] = []
        worker = threading.Thread(target=lambda: result.append(
            self.service.compact_command("user-one", "agent-one", self.chat, "/compact")))
        worker.start()
        self.assertTrue(started.wait(5))
        with self.assertRaises(Exception):  # a second /compact is refused while the first runs
            self.service.compact_command("user-one", "agent-one", self.chat, "/compact")
        events = self._send("during " + "q" * 100)  # queued behind the compaction
        release.set()
        worker.join(5)
        list(events)
        self._finish_workers()
        self.assertTrue(result[0].startswith("Context compacted"))
        self.assertIn("slow summary", self._context()[1]["content"])
        self.assertEqual(self._stored()[-1]["role"], "assistant")


if __name__ == "__main__":
    unittest.main()
