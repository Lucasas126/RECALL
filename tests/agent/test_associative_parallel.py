from __future__ import annotations

import copy
import threading
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from src.agent.agent import Agent
from src.agent import associative
from src.config.models import ChatConfig, ModelsConfig, ResolvedAgent
from src.core.sources.conversations import ConversationsSource
from src.providers.cancellation import CancellationToken, GenerationCancelled
from src.providers.provider import LLMProvider

HINT = "[associative memory] possibly-relevant fragments:\n- [notes:cat.md] the cat is called Misu"
SEARCHING = ("memory", {"state": "searching"})
FOUND = ("memory", {"state": "done", "matches": 1})
NOTHING = ("memory", {"state": "done", "matches": 0})
SKIPPED = ("memory", {"state": "skipped"})


class ParallelRecallTests(unittest.TestCase):
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
            tools=False, associative_memory="llm",
        )
        with (
            patch("src.agent.agent.SourceRegistry.for_agent", return_value=registry),
            patch("src.agent.agent.VectorIndex.for_agent"),
        ):
            self.agent = Agent(config, self.provider)
        self.contexts: list[list[dict]] = []

    def _model(self, first_call):
        """Provider double: `first_call` scripts the first call, later calls answer 'Continuation'."""
        def invoke(messages, model, *, cancellation: CancellationToken, **kwargs):
            self.contexts.append(copy.deepcopy(messages))
            if len(self.contexts) == 1:
                yield from first_call(cancellation)
                return
            yield "content", "Continuation"
            yield {"usage": {"prompt_tokens": 10, "completion_tokens": 5}}
        self.provider.invoke.side_effect = invoke

    @staticmethod
    def _wait_for_cancel(cancellation: CancellationToken, seconds: float = 3):
        """Stand-in for a model that is still generating: raises once the call is cancelled."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            cancellation.raise_if_cancelled()
            time.sleep(0.005)
        raise AssertionError("the call was never paused")

    def test_hit_pauses_the_reply_and_it_continues_with_the_hint(self) -> None:
        written = threading.Event()

        def first_call(cancellation):
            yield "content", "Partial."
            written.set()
            yield from self._wait_for_cancel(cancellation)

        def recall(agent, user_input, cancellation, history=None, chat_id=None):
            self.assertTrue(written.wait(3))
            return HINT, 1

        self._model(first_call)
        with patch("src.agent.agent.associative.recall", side_effect=recall):
            events = list(self.agent.invoke("What is my cat called?"))

        self.assertEqual(
            events,
            [
                SEARCHING, ("content", "Partial."), FOUND,
                ("content", "\n\n"), ("content", "Continuation"),
            ],
        )
        self.assertEqual(len(self.contexts), 2)
        self.assertEqual(self.contexts[1][-2], {"role": "assistant", "content": "Partial."})
        self.assertEqual(self.contexts[1][-1]["role"], "system")
        self.assertTrue(self.contexts[1][-1]["content"].startswith(HINT))
        self.assertIn("paused", self.contexts[1][-1]["content"])
        self.assertEqual(self.agent.last_reply, "Partial.\n\nContinuation")
        turns = self.conversations.load(self.agent.chat_id)
        self.assertEqual(turns[-1]["content"], "Partial.\n\nContinuation")
        self.assertEqual(turns[-1]["associative_matches"], 1)
        self.assertIn("associative memory (parallel):", self.agent.last_latency_report)

    def test_event_and_turn_carry_the_injected_fragments(self) -> None:
        written = threading.Event()
        fragment = {"address": "notes:cat.md", "label": "Cat", "score": 0.83, "excerpt": "the cat is called Misu"}

        def first_call(cancellation):
            yield "content", "Partial."
            written.set()
            yield from self._wait_for_cancel(cancellation)

        def recall(agent, user_input, cancellation, history=None, chat_id=None):
            self.assertTrue(written.wait(3))
            return associative.RecallResult(HINT, 1, [fragment], "cat name")

        self._model(first_call)
        with patch("src.agent.agent.associative.recall", side_effect=recall):
            events = list(self.agent.invoke("What is my cat called?"))

        self.assertIn(("memory", {"state": "done", "matches": 1, "fragments": [fragment], "query": "cat name"}), events)
        last = self.conversations.load(self.agent.chat_id)[-1]
        self.assertEqual(last["associative_fragments"], [fragment])
        self.assertEqual(last["associative_query"], "cat name")

    def test_no_match_turn_keeps_the_query_only(self) -> None:
        def reply(*args, **kwargs):
            yield "content", "Hi"
            yield {"usage": {"prompt_tokens": 10, "completion_tokens": 5}}

        self.provider.invoke.side_effect = reply
        with patch("src.agent.agent.associative.recall", return_value=associative.RecallResult(None, 0, query="pets")):
            list(self.agent.invoke("hello"))
        last = self.conversations.load(self.agent.chat_id)[-1]
        self.assertEqual(last["associative_matches"], 0)
        self.assertEqual(last["associative_query"], "pets")
        self.assertNotIn("associative_fragments", last)

    def test_hit_mid_sentence_waits_for_the_end_of_the_sentence(self) -> None:
        written = threading.Event()
        found = threading.Event()

        def first_call(cancellation):
            yield "content", "The cat is call"
            written.set()
            self.assertTrue(found.wait(3))
            time.sleep(0.05)  # the hit is ready but the sentence is not finished
            cancellation.raise_if_cancelled()
            yield "content", "ed Luna."
            yield from self._wait_for_cancel(cancellation)  # never reached: the pause happens at the full stop

        def recall(agent, user_input, cancellation, history=None, chat_id=None):
            self.assertTrue(written.wait(3))
            found.set()
            return HINT, 1

        self._model(first_call)
        with patch("src.agent.agent.associative.recall", side_effect=recall):
            events = list(self.agent.invoke("What is my cat called?"))

        self.assertEqual(
            events,
            [
                SEARCHING, ("content", "The cat is call"), ("content", "ed Luna."),
                FOUND, ("content", "\n\n"), ("content", "Continuation"),
            ],
        )
        self.assertEqual(self.contexts[1][-2], {"role": "assistant", "content": "The cat is called Luna."})
        self.assertEqual(self.agent.last_reply, "The cat is called Luna.\n\nContinuation")

    def test_hit_before_any_text_restarts_the_call_with_the_hint(self) -> None:
        started = threading.Event()

        def first_call(cancellation):
            started.set()
            yield from self._wait_for_cancel(cancellation)

        def recall(agent, user_input, cancellation, history=None, chat_id=None):
            self.assertTrue(started.wait(3))
            return HINT, 1

        self._model(first_call)
        with patch("src.agent.agent.associative.recall", side_effect=recall):
            events = list(self.agent.invoke("What is my cat called?"))

        self.assertEqual(events, [SEARCHING, FOUND, ("content", "Continuation")])
        self.assertEqual(self.contexts[1][-1], {"role": "system", "content": HINT})
        self.assertEqual(self.agent.last_reply, "Continuation")

    def test_hit_during_a_call_that_emits_reasoning_and_tools_at_its_end_loses_neither(self) -> None:
        # Claude subscription style: nothing streams until the call is done, so a hit that lands meanwhile
        # cancels the call before its reasoning and tool calls are read
        self.agent._tool_handlers = {"echo": lambda **kwargs: "echoed"}
        self.agent._tool_specs = [{"type": "function", "function": {"name": "echo", "parameters": {}}}]
        call = {"id": "c1", "name": "echo", "arguments": {"text": "hi"}}

        started = threading.Event()

        def first_call(cancellation):
            started.set()
            deadline = time.monotonic() + 3
            while not cancellation.is_cancelled() and time.monotonic() < deadline:
                time.sleep(0.005)
            self.assertTrue(cancellation.is_cancelled())
            yield "reasoning", "I should look."
            yield {"tool_calls": [call]}

        def recall(agent, user_input, cancellation, history=None, chat_id=None):
            self.assertTrue(started.wait(3))
            return HINT, 1

        self._model(first_call)
        with patch("src.agent.agent.associative.recall", side_effect=recall):
            events = list(self.agent.invoke("What is my cat called?"))

        kinds = [e[0] for e in events]
        self.assertEqual(kinds[:2], ["memory", "reasoning"])
        self.assertIn(("reasoning", "I should look."), events)
        self.assertEqual([e[1]["name"] for e in events if e[0] == "tool"], ["echo"])
        self.assertEqual(len(self.contexts), 2)  # the finished call was kept: no restart before the tool ran
        hints = [i for i, m in enumerate(self.contexts[1]) if m["role"] == "system" and m["content"].startswith(HINT)]
        self.assertEqual(len(hints), 1)  # the hint reaches the next hop, once, right after the user message
        self.assertEqual(self.contexts[1][hints[0] - 1]["role"], "user")
        self.assertEqual(self.contexts[1][-1]["role"], "tool")
        self.assertEqual(self.agent.last_reply, "Continuation")
        timeline = self.conversations.load(self.agent.chat_id)[-1]["timeline"]
        self.assertEqual([s["type"] for s in timeline], ["reasoning", "tool", "text"])

    def test_no_hit_never_pauses_the_reply(self) -> None:
        def first_call(cancellation):
            yield "content", "Answer"
            yield {"usage": {"prompt_tokens": 10, "completion_tokens": 5}}

        self._model(first_call)
        with patch("src.agent.agent.associative.recall", return_value=(None, 0)):
            events = list(self.agent.invoke("Hello"))

        self.assertEqual(events[0], SEARCHING)
        self.assertEqual([e for e in events if e[0] == "content"], [("content", "Answer")])
        self.assertEqual([e for e in events if e[0] == "memory"][1:], [NOTHING])
        self.assertEqual(len(self.contexts), 1)
        self.assertEqual(self.agent.last_reply, "Answer")
        self.assertEqual(self.conversations.load(self.agent.chat_id)[-1]["associative_matches"], 0)

    def test_failed_search_is_reported_as_skipped(self) -> None:
        def first_call(cancellation):
            yield "content", "Answer"
            yield {"usage": {"prompt_tokens": 10, "completion_tokens": 5}}

        self._model(first_call)
        with (
            patch("src.agent.agent.associative.recall", side_effect=RuntimeError("index offline")),
            self.assertLogs("recall.associative", level="ERROR"),
        ):
            events = list(self.agent.invoke("Hello"))

        self.assertEqual(events[0], SEARCHING)
        self.assertEqual([e for e in events if e[0] == "memory"][1:], [SKIPPED])
        self.assertEqual(self.agent.last_reply, "Answer")
        last = self.conversations.load(self.agent.chat_id)[-1]
        self.assertTrue(last["associative_skipped"])
        self.assertNotIn("associative_matches", last)

    def test_late_hit_is_cancelled_and_not_used(self) -> None:
        job_token: list[CancellationToken] = []

        def recall(agent, user_input, cancellation, history=None, chat_id=None):
            job_token.append(cancellation)
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                cancellation.raise_if_cancelled()
                time.sleep(0.005)
            return HINT, 1

        def first_call(cancellation):
            yield "content", "Answer"
            yield {"usage": {"prompt_tokens": 10, "completion_tokens": 5}}

        self._model(first_call)
        with patch("src.agent.agent.associative.recall", side_effect=recall):
            events = list(self.agent.invoke("Hello"))

        self.assertEqual(events, [SEARCHING, ("content", "Answer"), SKIPPED])
        self.assertEqual(len(self.contexts), 1)
        self.assertEqual(self.agent.last_reply, "Answer")
        self.assertEqual(self.agent._assoc_match_count, 0)
        self.assertTrue(job_token[0]._cancelled)
        last = self.conversations.load(self.agent.chat_id)[-1]
        self.assertTrue(last["associative_skipped"])
        self.assertNotIn("associative_matches", last)

    def test_sync_call_waits_for_the_search_before_the_model(self) -> None:
        self.provider.invoke.return_value = {"response": "Misu"}
        with patch("src.agent.agent.associative.recall", return_value=(HINT, 1)):
            self.assertEqual(self.agent.invoke("What is my cat called?", stream=False), "Misu")

        context = self.provider.invoke.call_args.args[0]
        self.assertEqual(context[-1], {"role": "system", "content": HINT})
        self.assertEqual(self.agent._assoc_match_count, 1)
        self.assertEqual(self.agent.memory_event(), FOUND[1])
        self.assertIn("associative memory:", self.agent.last_latency_report)
        self.assertNotIn("(parallel)", self.agent.last_latency_report)

    def test_hit_ready_before_the_call_goes_into_the_first_call(self) -> None:
        def first_call(cancellation):
            yield "content", "Misu"
            yield {"usage": {"prompt_tokens": 10, "completion_tokens": 5}}

        def recall(agent, user_input, cancellation, history=None, chat_id=None):
            return HINT, 1

        self._model(first_call)
        with patch("src.agent.agent.associative.recall", side_effect=recall):
            self.agent._assoc_job = None
            job_events = []
            original = Agent._stream_reply

            def wait_then_stream(agent, cancellation):
                agent._assoc_job.wait()  # the search wins the race against the model call
                yield from original(agent, cancellation)

            with patch.object(Agent, "_stream_reply", wait_then_stream):
                job_events = list(self.agent.invoke("What is my cat called?"))

        self.assertEqual(job_events, [SEARCHING, FOUND, ("content", "Misu")])
        self.assertEqual(len(self.contexts), 1)
        self.assertEqual(self.contexts[0][-1], {"role": "system", "content": HINT})

    def test_system_prompt_tells_the_model_about_mid_reply_hints(self) -> None:
        self.assertIn("[associative memory]", self.agent.messages[0]["content"])

    def test_question_only_searches_with_the_message_and_skips_the_query_model(self) -> None:
        self.agent.config.associative_memory = "question_only"
        self.agent.query_memory = Mock(return_value=[
            {"address": "notes:cat.md", "document": "the cat is called Misu", "score": 0.8},
        ])
        suggestion, count = associative.recall(self.agent, "  What is my cat called?  ", history=[])

        self.assertEqual(count, 1)
        self.assertIn("notes:cat.md", suggestion)
        self.assertEqual(self.agent.query_memory.call_args.args[0], "What is my cat called?")
        self.provider.invoke.assert_not_called()

    def test_question_only_without_text_searches_nothing(self) -> None:
        self.agent.config.associative_memory = "question_only"
        self.agent.query_memory = Mock()
        self.assertEqual(associative.recall(self.agent, "   ", history=[]), (None, 0))
        self.agent.query_memory.assert_not_called()

    def test_off_runs_no_search_and_sends_no_events_or_notice(self) -> None:
        self.agent.config.associative_memory = "off"
        self.agent.messages[0] = {"role": "system", "content": self.agent._system_prompt()}
        self.assertNotIn("[associative memory]", self.agent.messages[0]["content"])

        def first_call(cancellation):
            yield "content", "Answer"
            yield {"usage": {"prompt_tokens": 10, "completion_tokens": 5}}

        self._model(first_call)
        with patch("src.agent.agent.associative.recall") as recall:
            events = list(self.agent.invoke("Hello"))

        recall.assert_not_called()
        self.assertEqual(events, [("content", "Answer")])
        last = self.conversations.load(self.agent.chat_id)[-1]
        self.assertNotIn("associative_matches", last)
        self.assertNotIn("associative_skipped", last)

    def _sequential(self) -> None:
        self.agent.config.associative_execution = "sequential"
        self.agent.messages[0] = {"role": "system", "content": self.agent._system_prompt()}

    def test_sequential_waits_for_the_search_and_never_pauses_the_reply(self) -> None:
        self._sequential()
        self.assertNotIn("[associative memory]", self.agent.messages[0]["content"])

        def first_call(cancellation):
            yield "content", "Misu."
            yield "content", " Always."
            yield {"usage": {"prompt_tokens": 10, "completion_tokens": 5}}

        def recall(agent, user_input, cancellation, history=None, chat_id=None):
            time.sleep(0.1)  # slower than the model would have been
            return HINT, 1

        self._model(first_call)
        with patch("src.agent.agent.associative.recall", side_effect=recall):
            events = list(self.agent.invoke("What is my cat called?"))

        self.assertEqual(events, [SEARCHING, FOUND, ("content", "Misu."), ("content", " Always.")])
        self.assertEqual(len(self.contexts), 1)
        self.assertEqual(self.contexts[0][-1], {"role": "system", "content": HINT})
        self.assertEqual(self.agent.last_reply, "Misu. Always.")
        self.assertNotIn("(parallel)", self.agent.last_latency_report)
        self.assertIn("associative memory:", self.agent.last_latency_report)

    def test_sequential_late_search_delays_the_model_instead_of_being_skipped(self) -> None:
        self._sequential()
        model_started = []

        def first_call(cancellation):
            model_started.append(time.monotonic())
            yield "content", "Answer"
            yield {"usage": {"prompt_tokens": 10, "completion_tokens": 5}}

        def recall(agent, user_input, cancellation, history=None, chat_id=None):
            time.sleep(0.15)
            return None, 0

        self._model(first_call)
        started = time.monotonic()
        with patch("src.agent.agent.associative.recall", side_effect=recall):
            events = list(self.agent.invoke("Hello"))

        self.assertGreaterEqual(model_started[0] - started, 0.14)
        self.assertEqual(events, [SEARCHING, NOTHING, ("content", "Answer")])

    def test_cancelling_while_waiting_for_a_sequential_search_interrupts_the_turn(self) -> None:
        self._sequential()
        token = CancellationToken()
        job_token: list[CancellationToken] = []

        def recall(agent, user_input, cancellation, history=None, chat_id=None):
            job_token.append(cancellation)
            threading.Timer(0.05, token.cancel).start()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline:
                cancellation.raise_if_cancelled()
                time.sleep(0.005)
            return HINT, 1

        self._model(lambda cancellation: iter(()))
        with patch("src.agent.agent.associative.recall", side_effect=recall):
            with self.assertRaises(GenerationCancelled):
                list(self.agent.invoke("Hello", cancellation=token))

        self.assertEqual(self.contexts, [])
        self.assertTrue(self.conversations.load(self.agent.chat_id)[-1]["interrupted"])
        self.assertTrue(job_token[0]._cancelled)

    def test_execution_defaults_to_parallel_and_rejects_unknown_values(self) -> None:
        self.assertEqual(ChatConfig().associative_execution, "parallel")
        self.assertEqual(ChatConfig(associative_execution="sequential").associative_execution, "sequential")
        with self.assertRaises(ValueError):
            ChatConfig(associative_execution="later")

    def test_boolean_values_are_rejected_as_modes(self) -> None:
        fields = dict(
            user_id="u", id="a", name="A", models=ModelsConfig(),
            provider_name="ollama", api_key="", embeddings_provider_name="ollama", embeddings_api_key="",
        )
        for flag in (True, False):
            with self.assertRaises(ValueError):
                ResolvedAgent(**fields, associative_memory=flag)
            with self.assertRaises(ValueError):
                ChatConfig(associative_memory=flag)
        self.assertEqual(ResolvedAgent(**fields).associative_memory, "llm")
        self.assertEqual(ChatConfig(associative_memory="question_only").associative_memory, "question_only")
        with self.assertRaises(ValueError):
            ChatConfig(associative_memory="sometimes")


if __name__ == "__main__":
    unittest.main()
