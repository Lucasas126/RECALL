from __future__ import annotations

import json
import unittest

from src.agent import dream_compaction
from src.agent.dream import DreamRunner
from test_dream import DreamFixture, ScriptedProvider, _call, _config, _finish


class WindowProvider(ScriptedProvider):
    """A scripted model with a small context window; summary calls (no tools) answer with fixed notes."""

    def __init__(self, steps, window: int):
        super().__init__(steps)
        self.window = window
        self.summaries: list[str] = []

    def context_window(self, model):
        return self.window

    def invoke(self, messages, model, **kwargs):
        if kwargs.get("tools") is None:
            self.summaries.append(messages[1]["content"])
            return {"response": "- Read chat X up to turn 2; saved nothing yet; it says the user moved to Valencia."}
        return super().invoke(messages, model, **kwargs)


class DreamCompactionTests(DreamFixture):
    def events(self) -> list[dict]:
        path = self.root / "u" / "a" / "dream" / "sessions" / "20261103-040000-abcd.jsonl"
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def runner(self, provider, **dream) -> DreamRunner:
        return DreamRunner(_config(**dream), provider, self.sources, session_id="20261103-040000-abcd", trigger="manual")

    def test_warning_then_compaction_keeps_progress_and_saves_tokens(self) -> None:
        chat_id = self.chat(*["word " * 1500 for _ in range(6)])
        big = {"prompt_tokens": 7_000, "completion_tokens": 10}
        provider = WindowProvider([
            {"tool_calls": [_call("c1", "dream_pending")], "usage": {"prompt_tokens": 3_000, "completion_tokens": 5}},
            {"tool_calls": [_call("c2", "dream_read_chat", chat_id=chat_id, max_turns=3)], "usage": big},
            {"tool_calls": [_call("c3", "append", memory_path="mind_palace:journal/2026-11.md", content="- Valencia")],
             "usage": big},
            {"tool_calls": [_call("c4", "dream_read_chat", chat_id=chat_id)], "usage": {"prompt_tokens": 3_000, "completion_tokens": 5}},
            {"tool_calls": [_call("c5", "dream_mark_reviewed", chat_id=chat_id)]},
            _finish("done"),
        ], window=16_000)
        summary = self.runner(provider).run()
        self.assertEqual(summary["status"], "completed")
        kinds = [event["type"] for event in self.events()]
        self.assertIn("compaction_warning", kinds)
        self.assertIn("compacted", kinds)
        self.assertLess(kinds.index("compaction_warning"), kinds.index("compacted"))
        # the model got the save-now warning before compaction, and the call after it carries the notes
        warned = next(i for i, call in enumerate(provider.calls)
                      if any("compacted into short notes" in str(m.get("content")) for m in call["messages"]))
        after = provider.calls[warned + 1]["messages"]
        self.assertIn("[CONTEXT COMPACTED]", after[1]["content"])
        self.assertIn("moved to Valencia", after[1]["content"])
        self.assertIn(f"{chat_id}: 3", after[1]["content"])  # exact progress: read up to turn 3
        self.assertEqual(after[0]["role"], "system")
        self.assertGreaterEqual(len(provider.summaries), 1)
        # reading continued where it stopped and the chat ended reviewed
        self.assertEqual(summary["chats_reviewed"], [chat_id])
        self.assertEqual(self.queue.pending(), [])

    def test_no_compaction_when_the_context_stays_small(self) -> None:
        chat_id = self.chat("hi", "hello")
        provider = WindowProvider([
            {"tool_calls": [_call("c1", "dream_read_chat", chat_id=chat_id)], "usage": {"prompt_tokens": 500, "completion_tokens": 5}},
            {"tool_calls": [_call("c2", "dream_mark_reviewed", chat_id=chat_id)]},
            _finish("done"),
        ], window=200_000)
        self.runner(provider).run()
        kinds = [event["type"] for event in self.events()]
        self.assertNotIn("compaction_warning", kinds)
        self.assertNotIn("compacted", kinds)
        self.assertEqual(provider.summaries, [])

    def test_a_failed_summary_still_frees_the_context_with_the_progress_note(self) -> None:
        chat_id = self.chat("a", "b")

        class Failing(WindowProvider):
            def invoke(self, messages, model, **kwargs):
                if kwargs.get("tools") is None:
                    raise RuntimeError("summary down")
                return ScriptedProvider.invoke(self, messages, model, **kwargs)

        provider = Failing([
            {"tool_calls": [_call("c1", "dream_read_chat", chat_id=chat_id)], "usage": {"prompt_tokens": 9_000, "completion_tokens": 5}},
            {"tool_calls": [_call("c2", "dream_pending")], "usage": {"prompt_tokens": 9_000, "completion_tokens": 5}},
            {"tool_calls": [_call("c3", "dream_mark_reviewed", chat_id=chat_id)]},
            _finish("done"),
        ], window=10_000)
        summary = self.runner(provider).run()
        self.assertEqual(summary["status"], "completed")
        compacted = [event for event in self.events() if event["type"] == "compacted"]
        self.assertTrue(compacted)
        self.assertIn("summary down", compacted[0]["error"])

    def test_zero_max_tokens_means_no_cap(self) -> None:
        provider = WindowProvider([
            {"tool_calls": [_call("c1", "dream_pending")], "usage": {"prompt_tokens": 2_000_000, "completion_tokens": 1}},
            _finish("done"),
        ], window=10_000_000)
        summary = self.runner(provider, max_tokens=0).run()
        self.assertEqual(summary["status"], "completed")
        self.assertNotIn("wrap_up_warning", [event["type"] for event in self.events()])
        self.assertIn("no token cap", provider.calls[0]["messages"][1]["content"])

    def test_plan_keeps_system_opening_and_latest_step(self) -> None:
        messages = [
            {"role": "system", "content": "s"}, {"role": "user", "content": "o"},
            {"role": "assistant", "content": "", "tool_calls": []}, {"role": "tool", "content": "r1"},
            {"role": "assistant", "content": "", "tool_calls": []}, {"role": "tool", "content": "r2"},
        ]
        self.assertEqual(dream_compaction.plan(messages).tail_start, 4)
        self.assertIsNone(dream_compaction.plan(messages[:4]))


if __name__ == "__main__":
    unittest.main()
