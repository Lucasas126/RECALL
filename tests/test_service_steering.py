from __future__ import annotations

import copy
import re
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from src.agent.agent import Agent
from src.config.models import AgentConfig, ModelsConfig, ProviderConfig, ResolvedAgent, UserConfig
from src.core.sources.conversations import ConversationsSource
from src.providers.cancellation import CancellationToken
from src.providers.provider import LLMProvider
from src.service.chat import ChatService, UnknownAgent, UnknownChat, UnknownUser
from auth_helpers import TEST_PASSWORD_HASH


class SteeringFixture(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.sources: dict[tuple[str, str], ConversationsSource] = {}
        self.providers: dict[tuple[str, str], Mock] = {}
        self.calls: dict[tuple[str, str], list[dict]] = {}
        self.agents: dict[tuple[str, str], Agent] = {}
        self.gates: dict[str, tuple[threading.Event, threading.Event]] = {}
        self.memory = True
        self.associative_memory = False
        self.retained: int | None = None
        for user in ("user-one", "user-two"):
            for agent in ("agent-one", "agent-two"):
                key = (user, agent)
                root = self.root / user / agent
                root.mkdir(parents=True)
                self.sources[key] = ConversationsSource(root)
                self.calls[key] = []
                provider = Mock(spec=LLMProvider)
                provider.context_window.return_value = 10000
                provider.invoke.side_effect = self._responder(key)
                self.providers[key] = provider
        registry_patch = patch(
            "src.service.chat.SourceRegistry.for_agent", side_effect=self._registry,
        )
        user_patch = patch("src.service.chat.load_user", side_effect=self._user)
        index_patch = patch("src.agent.agent.VectorIndex.for_agent")
        agent_patch = patch(
            "src.service.chat.load_agent_config",
            side_effect=lambda user, agent: AgentConfig(
                id=agent, name=agent, personality_prompt="Test prompt",
            ),
        )
        for patcher in (registry_patch, user_patch, index_patch, agent_patch):
            patcher.start()
            self.addCleanup(patcher.stop)
        config_root = self.root / "config"
        for user in ("user-one", "user-two"):
            (config_root / user).mkdir(parents=True)
        config_patch = patch("src.service.chat.CONFIG_DIR", config_root)
        config_patch.start()
        self.addCleanup(config_patch.stop)
        self.service = ChatService(agent_factory=self._factory)
        self.addCleanup(self._finish_workers)
        self.chat = self.service.create_chat("user-one", "agent-one")["id"]

    def _finish_workers(self) -> None:
        for _, release in self.gates.values():
            release.set()
        for slot in self.service._slots.values():
            with slot.state:
                worker = slot.worker
            if worker is not None:
                worker.join(3)
                self.assertFalse(worker.is_alive(), "service worker did not finish")

    def _user(self, user_id: str) -> UserConfig:
        if user_id not in ("user-one", "user-two"):
            raise FileNotFoundError(user_id)
        return UserConfig(
            id=user_id, name="Test", provider=ProviderConfig(name="ollama"),
            password_hash=TEST_PASSWORD_HASH, agents=["agent-one", "agent-two"],
        )

    def _registry(self, user: str, agent: str) -> Mock:
        registry = Mock()
        registry.conversations.return_value = self.sources[(user, agent)]
        return registry

    def _factory(self, user: str, agent: str, chat_id: str | None) -> Agent:
        config = ResolvedAgent(
            user_id=user, id=agent, name="Test", personality_prompt="Test prompt",
            models=ModelsConfig(), provider_name="ollama", api_key="",
            embeddings_provider_name="ollama", embeddings_api_key="",
            tools=False, associative_memory=self.associative_memory, has_memory=self.memory,
            conversation_messages_retained=self.retained,
        )
        instance = Agent(config, self.providers[(user, agent)], chat_id)
        self.agents[(user, agent)] = instance
        return instance

    def _responder(self, key: tuple[str, str]):
        def respond(messages, model, *, cancellation: CancellationToken, **kwargs):
            snapshot = copy.deepcopy(messages)
            for m in snapshot:
                if m["role"] == "user" and isinstance(m["content"], str):
                    m["content"] = re.sub(r"^\[[^\]]*\] ?", "", m["content"])
            self.calls[key].append({"context": snapshot, "cancellation": cancellation})
            latest = next(m["content"] for m in reversed(snapshot) if m["role"] == "user")
            if latest == "fail":
                raise RuntimeError("provider unavailable")
            if latest in self.gates:
                started, release = self.gates[latest]
                yield "content", "Partial"
                started.set()
                if not release.wait(3):
                    raise RuntimeError("test generation was not released")
                cancellation.raise_if_cancelled()
            yield "content", "Answer: " + latest
            yield {"usage": {"prompt_tokens": 10, "completion_tokens": 5}}
        return respond

    def _gate(self, text: str) -> tuple[threading.Event, threading.Event]:
        gate = (threading.Event(), threading.Event())
        self.gates[text] = gate
        return gate

    def _send(self, text: str, *, chat: str | None = None, stream: bool = True):
        return self.service.send(
            "user-one", "agent-one", chat or self.chat, text, stream=stream,
        )

    def _context(self) -> list[dict]:
        return self.calls[("user-one", "agent-one")][-1]["context"]


class ServiceSteeringTests(SteeringFixture):
    def test_steers_without_consuming_the_previous_reply(self) -> None:
        started, release = self._gate("Original")
        old = self._send("Original")
        self.assertTrue(started.wait(2))
        new = self._send("Correction")
        release.set()
        old_events, new_events = list(old), list(new)
        self.assertEqual([e.kind for e in old_events], ["text", "interrupted"])
        self.assertEqual(old_events[-1].text, "Partial")
        self.assertEqual(old_events[-1].data, {"reasoning": "", "actions": []})
        self.assertEqual([e.kind for e in new_events], ["text", "usage"])
        self.assertEqual(new_events[0].text, "Answer: Correction")
        old_ids = {e.generation_id for e in old_events}
        new_ids = {e.generation_id for e in new_events}
        self.assertEqual(len(old_ids), 1)
        self.assertEqual(len(new_ids), 1)
        self.assertNotEqual(old_ids, new_ids)
        self.assertNotIn("", old_ids | new_ids)
        context = self._context()
        self.assertEqual(context[1]["content"], "Original")
        self.assertIn("[Response interrupted by user.]", context[2]["content"])
        self.assertEqual(context[3]["content"], "Correction")
        history = self.service.get_chat("user-one", "agent-one", self.chat)["messages"]
        self.assertTrue(history[1]["interrupted"])
        self.assertEqual(history[-1]["content"], "Answer: Correction")

    def test_burst_preserves_all_inputs_without_running_superseded_generations(self) -> None:
        started, release = self._gate("Original")
        first = self._send("Original")
        self.assertTrue(started.wait(2))
        second = self._send("Correction one")
        third = self._send("Correction two")
        fourth = self._send("Correction three")
        release.set()
        self.assertEqual(list(first)[-1].kind, "interrupted")
        self.assertEqual([e.kind for e in second], ["interrupted"])
        self.assertEqual([e.kind for e in third], ["interrupted"])
        self.assertEqual(list(fourth)[0].text, "Answer: Correction three")
        self.assertEqual(len(self.calls[("user-one", "agent-one")]), 2)
        users = [m["content"] for m in self._context() if m["role"] == "user"]
        self.assertEqual(users, ["Original", "Correction one", "Correction two", "Correction three"])

    def test_non_streaming_delivery_has_explicit_partial_interruption(self) -> None:
        started, release = self._gate("Original")
        first = self._send("Original", stream=False)
        self.assertTrue(started.wait(2))
        second = self._send("Correction", stream=False)
        release.set()
        events = list(first)
        self.assertEqual([e.kind for e in events], ["interrupted"])
        self.assertEqual(events[0].text, "Partial")
        self.assertEqual(list(second)[0].text, "Answer: Correction")

    def test_other_chat_runs_in_parallel_and_does_not_interrupt(self) -> None:
        started, release = self._gate("Original")
        other_started, other_release = self._gate("Other chat")
        first = self._send("Original")
        self.assertTrue(started.wait(2))
        other = self.service.create_chat("user-one", "agent-one")["id"]
        second = self._send("Other chat", chat=other)
        self.assertTrue(other_started.wait(2))
        release.set()
        other_release.set()
        self.assertNotIn("interrupted", [e.kind for e in first])
        second_events = list(second)
        self.assertEqual(
            [event.text for event in second_events if event.kind == "text"][-1],
            "Answer: Other chat",
        )
        contexts = [call["context"] for call in self.calls[("user-one", "agent-one")]]
        users = [[m["content"] for m in context if m["role"] == "user"] for context in contexts]
        self.assertIn(["Original"], users)
        self.assertIn(["Other chat"], users)

    def test_active_steering_stays_isolated_from_other_chat(self) -> None:
        self.memory = False
        started, release = self._gate("Original")
        first = self._send("Original")
        self.assertTrue(started.wait(2))
        other = self.service.create_chat("user-one", "agent-one")["id"]
        queued = self._send("Other chat", chat=other)
        correction = self._send("Correction")
        release.set()
        self.assertEqual(list(first)[-1].kind, "interrupted")
        self.assertEqual(list(correction)[0].text, "Answer: Correction")
        self.assertEqual(list(queued)[0].text, "Answer: Other chat")
        contexts = [call["context"] for call in self.calls[("user-one", "agent-one")]]
        users = [[m["content"] for m in c if m["role"] == "user"] for c in contexts]
        self.assertIn(["Original"], users)
        self.assertIn(["Original", "Correction"], users)
        self.assertIn(["Other chat"], users)

    def test_no_memory_and_small_retention_preserve_active_turn_only(self) -> None:
        self.memory = False
        self._assert_retains_active_turn()

    def test_small_retention_with_memory_preserves_active_turn_only(self) -> None:
        self._assert_retains_active_turn()

    def _assert_retains_active_turn(self) -> None:
        self.retained = 1
        started, release = self._gate("Original")
        first = self._send("Original")
        self.assertTrue(started.wait(2))
        second = self._send("Correction")
        release.set()
        list(first)
        list(second)
        users = [m["content"] for m in self._context() if m["role"] == "user"]
        self.assertEqual(users, ["Original", "Correction"])
        list(self._send("Fresh turn"))
        users = [m["content"] for m in self._context() if m["role"] == "user"]
        self.assertEqual(users, ["Fresh turn"])

    def test_interruption_reports_completed_tools_to_non_streaming_channel(self) -> None:
        started, release = self._gate("tool")
        key = ("user-one", "agent-one")
        regular = self._responder(key)

        def response(messages, model, **kwargs):
            latest = next(m["content"] for m in reversed(messages) if m["role"] == "user")
            if latest.endswith("Original"):
                yield {"tool_calls": [{"id": "write", "name": "write", "arguments": {}}]}
            else:
                yield from regular(messages, model, **kwargs)

        def write():
            started.set()
            self.assertTrue(release.wait(3))
            return "written"

        def factory(user, agent, chat):
            instance = self._factory(user, agent, chat)
            instance._tool_handlers = {"write": write}
            return instance

        self.providers[key].invoke.side_effect = response
        self.service._agent_factory = factory
        first = self._send("Original", stream=False)
        self.assertTrue(started.wait(2))
        second = self._send("Correction", stream=False)
        release.set()
        events = list(first)
        self.assertEqual([e.kind for e in events], ["interrupted"])
        self.assertEqual(events[0].data["actions"], [
            {"name": "write", "arguments": {}, "result": "written", "error": False},
        ])
        self.assertEqual(list(second)[0].text, "Answer: Correction")
        tool_results = [m for m in self._context() if m["role"] == "tool"]
        self.assertEqual(tool_results[0]["content"], "written")

    def test_other_users_and_agents_run_independently(self) -> None:
        started, release = self._gate("Original")
        first = self._send("Original")
        self.assertTrue(started.wait(2))
        for user, agent in (("user-two", "agent-one"), ("user-one", "agent-two")):
            chat = self.service.create_chat(user, agent)["id"]
            events = list(self.service.send(user, agent, chat, "Independent", stream=False))
            self.assertEqual(events[0].text, "Answer: Independent")
            users = [
                m["content"] for m in self.calls[(user, agent)][0]["context"] if m["role"] == "user"
            ]
            self.assertEqual(users, ["Independent"])
        self.calls[("user-one", "agent-one")][0]["cancellation"].raise_if_cancelled()
        release.set()
        self.assertNotIn("interrupted", [e.kind for e in first])

    def test_queued_empty_chats_are_not_pruned(self) -> None:
        started, release = self._gate("Original")
        first = self._send("Original")
        self.assertTrue(started.wait(2))
        queued_chat = self.service.create_chat("user-one", "agent-one")["id"]
        queued = self._send("Queued input", chat=queued_chat)
        empty = self.service.create_chat("user-one", "agent-one")["id"]
        chats = self.service.list_chats("user-one", "agent-one", keep=empty)
        self.assertIn(queued_chat, {chat["id"] for chat in chats})
        release.set()
        list(first)
        self.assertEqual(list(queued)[0].text, "Answer: Queued input")

    def test_validation_happens_before_scheduling(self) -> None:
        for args, error in [
            (("unknown-user", "agent-one", self.chat, "Hello"), UnknownUser),
            (("user-one", "unknown-agent", self.chat, "Hello"), UnknownAgent),
            (("user-one", "agent-one", "../invalid", "Hello"), UnknownChat),
            (("user-one", "agent-one", "missing", "Hello"), UnknownChat),
            (("user-one", "agent-one", self.chat, "  "), ValueError),
        ]:
            with self.subTest(args=args), self.assertRaises(error):
                self.service.send(*args)
        self.assertEqual(self.agents, {})

    def test_provider_errors_reach_consumer_and_worker_recovers(self) -> None:
        with self.assertLogs("recall.service", level="ERROR"):
            events = self._send("fail")
            with self.assertRaisesRegex(RuntimeError, "provider unavailable"):
                list(events)
        self.assertEqual(list(self._send("Recovery"))[0].text, "Answer: Recovery")

    def test_committed_reply_is_not_reclassified_as_interrupted(self) -> None:
        self.memory = False
        committed, release = threading.Event(), threading.Event()
        original_track = Agent._track_usage

        def track(agent: Agent, *args):
            if agent.last_reply == "Answer: Original":
                committed.set()
                self.assertTrue(release.wait(3))
            original_track(agent, *args)

        with patch.object(Agent, "_track_usage", track):
            first = self._send("Original")
            self.assertTrue(committed.wait(2))
            second = self._send("Fresh turn")
            release.set()
            self.assertNotIn("interrupted", [e.kind for e in first])
            self.assertNotIn("interrupted", [e.kind for e in second])
        self.assertEqual(
            [m["content"] for m in self._context() if m["role"] == "user"], ["Fresh turn"],
        )


if __name__ == "__main__":
    unittest.main()
