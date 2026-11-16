from __future__ import annotations

import threading
import unittest

from tests.tasks import test_service_steering as steering
from src.providers.cancellation import GenerationCancelled


class LiveToolStepTests(steering.SteeringFixture):
    """`tool_start` precedes each tool run; the final `tool` event carries the same call id."""

    def use_tools(self, calls: list[dict], handlers: dict) -> None:
        key = ("user-one", "agent-one")
        regular = self._responder(key)

        def response(messages, model, **kwargs):
            if any(m["role"] == "tool" for m in messages):
                yield from regular(messages, model, **kwargs)
            else:
                yield {"tool_calls": calls}

        def factory(user, agent, chat):
            instance = self._factory(user, agent, chat)
            instance._tool_handlers = handlers
            return instance

        self.providers[key].invoke.side_effect = response
        self.service._agent_factory = factory

    def tool_events(self, events) -> list[tuple[str, str]]:
        return [(e.kind, e.data["id"]) for e in events if e.kind in ("tool_start", "tool")]

    def test_single_tool_start_precedes_final_with_same_id(self) -> None:
        self.use_tools([{"id": "c1", "name": "write", "arguments": {"path": "a"}}], {"write": lambda path: "ok"})
        events = list(self._send("Go"))
        self.assertEqual(self.tool_events(events), [("tool_start", "c1"), ("tool", "c1")])
        start = next(e for e in events if e.kind == "tool_start")
        final = next(e for e in events if e.kind == "tool")
        self.assertEqual(start.data, {"id": "c1", "name": "write", "arguments": {"path": "a"}})
        self.assertEqual((final.data["result"], final.data["error"]), ("ok", False))

    def test_multiple_and_failing_tools_run_in_order_with_matching_ids(self) -> None:
        def boom():
            raise RuntimeError("nope")

        self.use_tools(
            [{"id": "a", "name": "write", "arguments": {}}, {"id": "b", "name": "boom", "arguments": {}}],
            {"write": lambda: "ok", "boom": boom},
        )
        events = list(self._send("Go"))
        self.assertEqual(
            self.tool_events(events), [("tool_start", "a"), ("tool", "a"), ("tool_start", "b"), ("tool", "b")],
        )
        failed = [e for e in events if e.kind == "tool"][1]
        self.assertTrue(failed.data["error"])
        self.assertLess(
            [e.kind for e in events].index("tool"), [e.kind for e in events].index("text"),
        )

    def test_stored_actions_do_not_carry_the_call_id(self) -> None:
        self.use_tools([{"id": "c1", "name": "write", "arguments": {}}], {"write": lambda: "ok"})
        list(self._send("Go"))
        self.assertEqual(self.agents[("user-one", "agent-one")].last_actions, [
            {"name": "write", "arguments": {}, "result": "ok", "error": False},
        ])

    def test_counter_ids_when_the_provider_gives_none(self) -> None:
        self.use_tools([], {"write": lambda: "ok"})
        agent = self._factory("user-one", "agent-one", self.chat)
        agent._tool_handlers = {"write": lambda: "ok"}
        events = list(agent._iter_tools([{"id": "", "name": "write", "arguments": "{}"}, {"id": None, "name": "write", "arguments": {}}]))
        self.assertEqual([(k, p["id"]) for k, p in events], [
            ("tool_start", "call-1"), ("tool", "call-1"), ("tool_start", "call-2"), ("tool", "call-2"),
        ])

    def test_cancelled_tool_leaves_start_then_interrupted_never_a_silent_end(self) -> None:
        def cancelled():
            raise GenerationCancelled()

        self.use_tools([{"id": "c1", "name": "search", "arguments": {}}], {"search": cancelled})
        events = list(self._send("Go"))
        kinds = [e.kind for e in events]
        self.assertEqual(kinds[0], "tool_start")
        self.assertNotIn("tool", kinds)
        self.assertEqual(kinds[-1], "interrupted")  # the client turns the unfinished start into "stopped"

    def test_follow_replays_tool_start_while_the_tool_runs(self) -> None:
        started, release = threading.Event(), threading.Event()

        def slow():
            started.set()
            self.assertTrue(release.wait(3))
            return "done"

        self.use_tools([{"id": "c1", "name": "read", "arguments": {}}], {"read": slow})
        original = self._send("Go")
        consumer = threading.Thread(target=lambda: list(original), daemon=True)
        consumer.start()
        self.assertTrue(started.wait(2))
        viewer = self.service.follow("user-one", "agent-one", self.chat)
        first = next(viewer)
        self.assertEqual((first.kind, first.data["id"]), ("tool_start", "c1"))
        release.set()
        rest = list(viewer)
        self.assertEqual([e.kind for e in rest][:1], ["tool"])
        self.assertEqual(rest[0].data["id"], "c1")
        consumer.join(3)

    def test_text_deltas_after_a_tool_hop_stay_separate_live_events(self) -> None:
        """Regression: the answer streams on every hop; nothing between provider and subscriber merges it."""
        key = ("user-one", "agent-one")

        def response(messages, model, **kwargs):
            if any(m["role"] == "tool" for m in messages):
                yield ("reasoning", "think ")
                for piece in ("Hel", "lo ", "there"):
                    yield ("content", piece)
                yield {"response": "Hello there", "usage": None, "tool_calls": None}
            else:
                yield {"tool_calls": [{"id": "c1", "name": "write", "arguments": {}}]}

        def factory(user, agent, chat):
            instance = self._factory(user, agent, chat)
            instance._tool_handlers = {"write": lambda: "ok"}
            return instance

        self.providers[key].invoke.side_effect = response
        self.service._agent_factory = factory
        events = list(self._send("Go"))
        self.assertEqual([e.text for e in events if e.kind == "text"], ["Hel", "lo ", "there"])
        kinds = [e.kind for e in events]
        self.assertLess(kinds.index("tool"), kinds.index("text"))


if __name__ == "__main__":
    unittest.main()
