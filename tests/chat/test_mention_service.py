from __future__ import annotations

import threading
import time
import unittest

from tests.tasks import test_service_steering as steering

A = ("user-one", "agent-one")
B = ("user-one", "agent-two")


class MentionServiceTests(steering.SteeringFixture):
    def _wait_copy(self, count: int) -> list[dict]:
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            turns = [t for t in self.sources[A].load(self.chat) if t.get("author_agent")]
            if len(turns) >= count:
                return turns
            time.sleep(0.02)
        self.fail("the mention reply was not copied into the origin chat")

    def test_mentioned_agent_answers_instead_of_the_chat_agent(self) -> None:
        list(self._send("Hello"))
        calls_a = len(self.calls[A])
        events = list(self._send("@agent-two what do you think?"))
        self.assertEqual(len(self.calls[A]), calls_a, "the chat's own agent must not run")
        self.assertEqual(len(self.calls[B]), 1)
        self.assertTrue(self.agents[B].chat_id)
        prompt = next(m["content"] for m in reversed(self.calls[B][-1]["context"]) if m["role"] == "user")
        self.assertIn("called from the chat of agent", prompt)
        self.assertIn("<untrusted_context", prompt)
        self.assertIn("user: Hello", prompt)
        self.assertTrue(prompt.endswith("what do you think?"))
        self.assertNotIn("@agent-two", prompt)
        text = "".join(e.text for e in events if e.kind == "text")
        self.assertTrue(text.startswith("Answer: "))
        copied = self._wait_copy(1)[-1]
        self.assertEqual(copied["author_agent"], "agent-two")
        self.assertEqual(copied["content"], text)
        user_turn = [t for t in self.sources[A].load(self.chat) if t["role"] == "user"][-1]
        self.assertEqual(user_turn["content"], "@agent-two what do you think?")
        self.assertEqual(user_turn["mention"]["agent_id"], "agent-two")
        b_chat = copied["mention_reply"]["chat_id"]
        self.assertEqual(self.sources[B].origin(b_chat), "mention")
        b_turns = self.sources[B].load(b_chat)
        self.assertEqual(b_turns[0]["mention_from"], {"agent_id": "agent-one", "chat_id": self.chat})

    def test_chat_is_reused_per_origin_chat_and_reply_is_labeled_for_a(self) -> None:
        list(self._send("@agent-two first"))
        first = self._wait_copy(1)[-1]["mention_reply"]["chat_id"]
        list(self._send("@agent-two second"))
        second = self._wait_copy(2)[-1]["mention_reply"]["chat_id"]
        self.assertEqual(first, second)
        other = self.service.create_chat("user-one", "agent-one")["id"]
        list(self._send("@agent-two third", chat=other))
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not any(t.get("author_agent") for t in self.sources[A].load(other)):
            time.sleep(0.02)
        third = next(t for t in self.sources[A].load(other) if t.get("author_agent"))["mention_reply"]["chat_id"]
        self.assertNotEqual(first, third)
        list(self._send("and you?"))
        labeled = [m for m in self._context() if m["role"] == "assistant" and "[reply from agent agent-two]" in m["content"]]
        self.assertEqual(len(labeled), 2)

    def test_unknown_or_own_id_is_a_normal_message(self) -> None:
        for text in ("@nobody hi", "@agent-one hi", "hi @agent-two"):
            list(self._send(text))
        self.assertEqual(len(self.calls[B]), 0)
        self.assertEqual(len(self.calls[A]), 3)

    def test_edits_and_task_messages_never_mention(self) -> None:
        list(self._send("Hello"))
        list(self.service.send("user-one", "agent-one", self.chat, "@agent-two edited", rewind_to=0))
        self.assertEqual(len(self.calls[B]), 0)


if __name__ == "__main__":
    unittest.main()
