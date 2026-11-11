from __future__ import annotations

import unittest

import test_service_steering as steering
from src.agent import agent as agent_module
from src.config.models import MAX_TOOL_HOPS_LIMIT, ChatConfig


class HopsWarningTests(unittest.TestCase):
    def test_warns_only_when_two_rounds_remain(self) -> None:
        notes = [agent_module._hops_warning(hop, 5) for hop in range(6)]
        self.assertEqual([i for i, note in enumerate(notes) if note], [3])
        self.assertIn("2 tool rounds left", notes[3])
        self.assertIn("3 used of 5", notes[3])

    def test_tiny_limits_are_not_warned(self) -> None:
        self.assertTrue(all(agent_module._hops_warning(hop, 2) is None for hop in range(3)))

    def test_override_is_validated(self) -> None:
        self.assertIsNone(ChatConfig().max_tool_hops)
        ChatConfig.model_validate({"max_tool_hops": MAX_TOOL_HOPS_LIMIT})
        for bad in (0, MAX_TOOL_HOPS_LIMIT + 1):
            with self.subTest(value=bad), self.assertRaises(ValueError):
                ChatConfig.model_validate({"max_tool_hops": bad})


class AgentHopsTests(steering.SteeringFixture):
    def run_turn(self, hops: int | None) -> list[list[dict]]:
        key = ("user-one", "agent-one")
        regular = self._responder(key)
        seen: list[list[dict]] = []

        def response(messages, model, **kwargs):
            seen.append([dict(m) for m in messages])
            if kwargs.get("tools") is None and any(m["role"] == "system" and m["content"] == agent_module._FINAL_NUDGE for m in messages):
                yield from regular(messages, model, **kwargs)
            else:
                yield {"tool_calls": [{"id": f"c{len(seen)}", "name": "write", "arguments": {}}]}

        def factory(user, agent, chat):
            instance = self._factory(user, agent, chat)
            instance._tool_handlers = {"write": lambda: "ok"}
            instance._tool_specs = [{"name": "write"}]
            instance.config.max_tool_hops = hops
            return instance

        self.providers[key].invoke.side_effect = response
        self.service._agent_factory = factory
        list(self._send("Go"))
        return seen

    def test_agent_override_sets_the_number_of_rounds_and_warns_before_the_cut(self) -> None:
        seen = self.run_turn(4)
        self.assertEqual(len(seen), 5)  # 4 tool rounds + the final answer
        warned = [i for i, ctx in enumerate(seen) if any("Tool budget" in str(m["content"]) for m in ctx)]
        self.assertEqual(warned, [2])
        self.assertTrue(any(m["content"] == agent_module._FINAL_NUDGE for m in seen[-1]))

    def test_without_override_the_application_value_applies(self) -> None:
        seen = self.run_turn(None)
        self.assertEqual(len(seen), agent_module.AGENT_MAX_TOOL_HOPS + 1)


if __name__ == "__main__":
    unittest.main()
