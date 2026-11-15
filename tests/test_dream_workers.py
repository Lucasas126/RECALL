from __future__ import annotations

import json
import threading
import unittest
from unittest.mock import patch

from src.agent.dream import DreamRunner, delegation_allowed
from src.config.models import AgentConfig, DreamConfig, ModelsConfig, ResolvedAgent
from test_dream import DreamFixture, _call, _finish


def _config(level: str = "collaborator", groups=("memory_read", "memory_edit", "self", "delegate"), **dream):
    return ResolvedAgent(
        user_id="u", id="a", name="Aria", models=ModelsConfig(chat="vendor/chat"),
        provider_name="ollama", api_key="", embeddings_provider_name="ollama", embeddings_api_key="",
        dream=DreamConfig(**dream), tool_groups=list(groups), level=level,
    )


class RoutedProvider:
    """Routes calls: worker calls (their system prompt names a 'reading worker') get the worker script per chat;
    the dream's own calls follow `dream_steps`."""

    def __init__(self, dream_steps: list, worker_reply: str = "## Chat\n- The user moved to Valencia (turn 1)"):
        self.dream_steps = list(dream_steps)
        self.worker_reply = worker_reply
        self.lock = threading.Lock()
        self.worker_calls: list[list[dict]] = []
        self.closing = "never called"
        self.dream_calls: list[list[dict]] = []

    def context_window(self, model):
        return 200_000

    def invoke(self, messages, model, **kwargs):
        if "reading worker" in str(messages[0].get("content")):
            with self.lock:
                self.worker_calls.append(messages)
            last = messages[-1]
            if last.get("role") == "user" and "budget is used up" in str(last.get("content")):
                self.closing = kwargs.get("tools")  # the closing call must not offer tools
                return {"response": "## Notes so far\n- read up to the limit (turn 2)",
                        "usage": {"prompt_tokens": 100, "completion_tokens": 20}}
            if last.get("role") == "tool":
                part = json.loads(last["content"])
                if part["remaining_after"] > 0:
                    return {"tool_calls": [{"id": "w", "name": "read_chat_part", "arguments": "{}"}],
                            "usage": {"prompt_tokens": 100, "completion_tokens": 5}}
                return {"response": self.worker_reply, "usage": {"prompt_tokens": 100, "completion_tokens": 50}}
            return {"tool_calls": [{"id": "w", "name": "read_chat_part", "arguments": json.dumps({"max_turns": 2})}],
                    "usage": {"prompt_tokens": 100, "completion_tokens": 5}}
        self.dream_calls.append(messages)
        step = self.dream_steps.pop(0) if self.dream_steps else _finish("done")
        return step(messages) if callable(step) else step


class DreamWorkerTests(DreamFixture):
    def runner(self, provider, config, worker_provider=None) -> DreamRunner:
        return DreamRunner(config, provider, self.sources, session_id="20261103-040000-abcd", trigger="manual",
                           worker_provider=worker_provider)

    def test_workers_can_run_on_their_own_model(self) -> None:
        chat = self.chat("I moved to Valencia", "Noted")
        dream_side = RoutedProvider([
            {"tool_calls": [_call("d1", "dream_delegate", chat_ids=[chat])]},
            {"tool_calls": [_call("m1", "dream_mark_reviewed", chat_id=chat)]},
            _finish("done"),
        ])
        workers = RoutedProvider([])
        models: list[str] = []
        original = workers.invoke

        def spy(messages, model, **kwargs):
            models.append(model)
            return original(messages, model, **kwargs)

        workers.invoke = spy
        config = _config(delegate=True, model="vendor/big", delegate_model="vendor/small")
        summary = self.runner(dream_side, config, worker_provider=workers).run()
        self.assertEqual(summary["status"], "completed")
        self.assertEqual(dream_side.worker_calls, [])  # no worker call went to the dream's provider
        self.assertTrue(models and set(models) == {"vendor/small"})
        self.assertEqual(summary["chats_reviewed"], [chat])

    def test_without_delegate_model_workers_use_the_dream_model(self) -> None:
        runner = self.runner(RoutedProvider([]), _config(delegate=True, model="vendor/big"))
        self.assertEqual(runner.worker_model, "vendor/big")

    def test_delegation_needs_option_group_and_level(self) -> None:
        self.assertTrue(delegation_allowed(_config(delegate=True)))
        self.assertFalse(delegation_allowed(_config(delegate=False)))
        self.assertFalse(delegation_allowed(_config(groups=("memory_read", "memory_edit"), delegate=True)))
        self.assertFalse(delegation_allowed(_config(level="assistant", groups=("memory_read",), delegate=True)))

    def test_tool_is_offered_only_when_allowed(self) -> None:
        for allowed in (True, False):
            provider = RoutedProvider([_finish("done")])
            self.runner(provider, _config(delegate=allowed)).run()
            names = {spec["function"]["name"] for spec in provider_tools(provider)}
            self.assertEqual("dream_delegate" in names, allowed)

    def test_workers_read_to_the_end_and_the_dream_can_mark(self) -> None:
        first = self.chat("I moved to Valencia", "Noted", "And I like tea", "OK", "bye")
        second = self.chat("hello", "hi")
        provider = RoutedProvider([
            {"tool_calls": [_call("d1", "dream_delegate", chat_ids=[first, second], focus="where the user lives")]},
            {"tool_calls": [
                _call("m1", "dream_mark_reviewed", chat_id=first),
                _call("m2", "dream_mark_reviewed", chat_id=second),
            ]},
            _finish("done"),
        ])
        summary = self.runner(provider, _config(delegate=True)).run()
        self.assertEqual(summary["status"], "completed")
        self.assertEqual(sorted(summary["chats_reviewed"]), sorted([first, second]))
        self.assertEqual(self.queue.pending(), [])
        # the report reached the dream as untrusted data, with the notes and the mark hint
        report = next(m for m in provider.dream_calls[1] if m.get("role") == "tool")["content"]
        self.assertIn("moved to Valencia", report)
        self.assertIn("read to its end", report)
        self.assertIn('<chat_text untrusted="true">', report)
        # workers got the goal and the focus, and their tokens count in the session
        self.assertTrue(any("where the user lives" in str(call[0]["content"]) for call in provider.worker_calls))
        self.assertIn("NOT to summarise", str(provider.worker_calls[0][0]["content"]))
        self.assertGreater(summary["tokens"]["total"], 0)

    def test_workers_cannot_write(self) -> None:
        chat = self.chat("a", "b")

        class Writer(RoutedProvider):
            def invoke(self, messages, model, **kwargs):
                if "reading worker" in str(messages[0].get("content")) and messages[-1].get("role") == "user":
                    tools = {spec["function"]["name"] for spec in kwargs.get("tools") or []}
                    self.offered = tools
                    return {"tool_calls": [{"id": "x", "name": "write", "arguments": json.dumps(
                        {"memory_path": "mind_palace:x.md", "content": "hacked"})}]}
                return super().invoke(messages, model, **kwargs)

        provider = Writer([{"tool_calls": [_call("d1", "dream_delegate", chat_ids=[chat])]}, _finish("done")])
        self.runner(provider, _config(delegate=True)).run()
        self.assertNotIn("write", provider.offered)
        self.assertNotIn("dream_mark_reviewed", provider.offered)
        self.assertNotIn("dream_delegate", provider.offered)
        self.assertFalse((self.root / "u" / "a" / "mind_palace" / "x.md").exists())

    def test_the_worker_guide_is_in_the_prompt_only_when_allowed(self) -> None:
        for allowed in (True, False):
            provider = RoutedProvider([_finish("done")])
            self.runner(provider, _config(delegate=allowed, prompt="Custom dream prompt.")).run()
            system = provider.dream_calls[0][0]["content"]
            self.assertIn("Custom dream prompt.", system)
            self.assertEqual("### Reading workers" in system, allowed)

    def test_workers_reread_reviewed_chats_from_a_turn_and_nothing_is_marked(self) -> None:
        chat = self.chat("I moved to Valencia", "Noted", "And I like tea", "OK", "bye", "ciao")
        self.queue.mark_reviewed(chat, 6)
        provider = RoutedProvider([
            {"tool_calls": [_call("d1", "dream_delegate", chat_ids=[chat], from_turn=3)]},
            {"tool_calls": [_call("m1", "dream_mark_reviewed", chat_id=chat)]},
            _finish("done"),
        ])
        summary = self.runner(provider, _config(delegate=True)).run()
        parts = [json.loads(m["content"]) for call in provider.worker_calls for m in call if m.get("role") == "tool"]
        self.assertEqual(parts[0]["turns"][0]["n"], 3)
        self.assertEqual(sorted({turn["n"] for part in parts for turn in part["turns"]}), [3, 4, 5, 6])
        report = next(m for m in provider.dream_calls[1] if m.get("role") == "tool")["content"]
        self.assertIn("reread to its end; nothing to mark", report)
        tool_results = [m["content"] for m in provider.dream_calls[-1] if m.get("role") == "tool"]
        self.assertTrue(tool_results[1].startswith("error: read"))  # the mark after the reread is refused
        self.assertEqual(summary["chats_reviewed"], [])
        self.assertEqual(self.queue.watermarks()[chat], 6)

    def test_a_reread_needs_an_existing_chat(self) -> None:
        provider = RoutedProvider([{"tool_calls": [_call("d1", "dream_delegate", chat_ids=["nope"], from_turn=1)]},
                                   _finish("done")])
        self.runner(provider, _config(delegate=True)).run()
        result = next(m for m in provider.dream_calls[1] if m.get("role") == "tool")["content"]
        self.assertTrue(result.startswith("error: chat not found"))

    def test_a_worker_stopped_by_its_token_limit_still_writes_notes(self) -> None:
        chat = self.chat("I moved to Valencia", "Noted", "And I like tea", "OK", "bye", "ciao")
        provider = RoutedProvider([{"tool_calls": [_call("d1", "dream_delegate", chat_ids=[chat])]}, _finish("done")])
        with patch("src.agent.dream_workers.WORKER_MAX_TOKENS", 150):  # two reading steps, then the limit
            self.runner(provider, _config(delegate=True)).run()
        report = next(m for m in provider.dream_calls[1] if m.get("role") == "tool")["content"]
        self.assertIn("read up to the limit (turn 2)", report)
        self.assertNotIn("(no notes)", report)
        self.assertIn("token limit", report)
        self.assertIsNone(provider.closing)
        last = provider.worker_calls[-1][-1]["content"]
        self.assertIn("budget is used up", last)  # the closing call was made, without tools

    def test_only_pending_chats_can_be_delegated(self) -> None:
        provider = RoutedProvider([{"tool_calls": [_call("d1", "dream_delegate", chat_ids=["nope"])]}, _finish("done")])
        self.runner(provider, _config(delegate=True)).run()
        result = next(m for m in provider.dream_calls[1] if m.get("role") == "tool")["content"]
        self.assertTrue(result.startswith("error: not pending"))


def provider_tools(provider: RoutedProvider) -> list[dict]:
    return provider.last_tools if hasattr(provider, "last_tools") else _captured.get(id(provider), [])


_captured: dict[int, list[dict]] = {}
_original = RoutedProvider.invoke


def _capture(self, messages, model, **kwargs):
    if "reading worker" not in str(messages[0].get("content")):
        _captured[id(self)] = kwargs.get("tools") or []
    return _original(self, messages, model, **kwargs)


RoutedProvider.invoke = _capture


if __name__ == "__main__":
    unittest.main()
