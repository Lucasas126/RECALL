from __future__ import annotations

import unittest
from types import SimpleNamespace

from src.agent import agent as agent_module

HINT = "possibly relevant: fragment"


def build(messages: list[dict], partial: str = "") -> SimpleNamespace:
    return SimpleNamespace(
        messages=messages, _truncated=False, _summary=None, _partial=partial, _assoc_suggestion=HINT,
    )


def context(fake: SimpleNamespace) -> list[dict]:
    return agent_module.Agent._context(fake)


class HintPositionTests(unittest.TestCase):
    def rounds(self, count: int) -> list[dict]:
        messages = [{"role": "system", "content": "prompt"}, {"role": "user", "content": "old"}, {"role": "assistant", "content": "older"}]
        messages.append({"role": "user", "content": "now"})
        for n in range(count):
            messages.append({"role": "assistant", "content": "", "tool_calls": [{"id": f"c{n}"}]})
            messages.append({"role": "tool", "tool_call_id": f"c{n}", "name": "x", "content": "ok"})
        return messages

    def test_hint_follows_the_turn_user_message_once_whatever_the_rounds(self) -> None:
        for rounds in (0, 1, 4):
            with self.subTest(rounds=rounds):
                ctx = context(build(self.rounds(rounds)))
                hints = [i for i, m in enumerate(ctx) if m["content"] == HINT]
                self.assertEqual(len(hints), 1)
                self.assertEqual(ctx[hints[0] - 1], {"role": "user", "content": "now"})
                self.assertEqual(len(ctx), len(self.rounds(rounds)) + 1)

    def test_prefix_is_stable_across_rounds(self) -> None:
        short = context(build(self.rounds(1)))
        longer = context(build(self.rounds(3)))
        self.assertEqual(longer[: len(short) - 2], short[:-2])

    def test_continuation_after_a_pause_keeps_the_hint_at_the_end(self) -> None:
        ctx = context(build(self.rounds(0), partial="Half "))
        self.assertEqual(ctx[-2], {"role": "assistant", "content": "Half"})
        self.assertEqual(ctx[-1]["role"], "system")
        self.assertTrue(ctx[-1]["content"].startswith(HINT))
        self.assertEqual(sum(1 for m in ctx if HINT in m["content"]), 1)

    def test_no_hint_leaves_the_context_alone(self) -> None:
        fake = build(self.rounds(2))
        fake._assoc_suggestion = None
        self.assertEqual(context(fake), fake.messages)


if __name__ == "__main__":
    unittest.main()
