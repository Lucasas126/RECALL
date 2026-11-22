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
        title = next(c for c in self.service.list_chats("user-one", "agent-two", include_hidden=True) if c["id"] == first)["title"]
        self.assertTrue(title.startswith("From ") and title.endswith(": first"), title)
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

    def test_stream_starts_with_the_speaker_and_history_names_the_author(self) -> None:
        events = list(self._send("@agent-two hi"))
        self.assertEqual(events[0].kind, "speaker")
        self.assertEqual(events[0].data["agent_id"], "agent-two")
        self.assertEqual(events[0].data["name"], "agent-two")
        self._wait_copy(1)
        messages = self.service.get_chat("user-one", "agent-one", self.chat)["messages"]
        self.assertEqual(messages[0]["mention"]["agent_id"], "agent-two")
        self.assertEqual((messages[1]["author_agent"], messages[1]["author_name"]), ("agent-two", "agent-two"))

    def test_running_mention_shows_as_active_can_be_followed_and_stopped(self) -> None:
        started, release = self._gate(self._b_prompt_key("slow"))
        events = self._send("@agent-two slow")
        consumer = threading.Thread(target=lambda: self.collected.extend(events))
        self.collected: list = []
        consumer.start()
        self.assertTrue(started.wait(2))
        self.assertTrue(self.service.chat_active("user-one", "agent-one", self.chat))
        follower = self.service.follow("user-one", "agent-one", self.chat)
        self.assertIsNotNone(follower)
        self.assertEqual(next(follower).kind, "speaker")
        self.service.stop_chat("user-one", "agent-one", self.chat)
        release.set()
        consumer.join(3)
        self.assertEqual(self.collected[-1].kind, "interrupted")
        copied = self._wait_copy(1)[-1]
        self.assertTrue(copied.get("interrupted"))
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and self.service.chat_active("user-one", "agent-one", self.chat):
            time.sleep(0.02)
        self.assertFalse(self.service.chat_active("user-one", "agent-one", self.chat))

    def test_approvals_of_the_mentioned_agent_are_answered_in_its_own_chat(self) -> None:
        from types import SimpleNamespace
        from src.terminal.approvals import ApprovalNotFound
        started, release = self._gate(self._b_prompt_key("tool"))
        events = self._send("@agent-two tool")
        consumer = threading.Thread(target=lambda: list(events))
        consumer.start()
        self.assertTrue(started.wait(2))
        b_chat = self.service._mention_run("user-one", "agent-one", self.chat)[1]
        emitted: list = []
        run = SimpleNamespace(emit=lambda kind, data: emitted.append(data), cancellation=None)
        outcome: list = []
        asker = threading.Thread(target=lambda: outcome.append(self.service._approvals.request(
            "user-one", "agent-two", b_chat, run, "terminal", "ls", "~", "look", "low",
        )))
        asker.start()
        deadline = time.monotonic() + 3
        while not emitted and time.monotonic() < deadline:
            time.sleep(0.01)
        card = emitted[0]
        self.assertEqual((card["agent_id"], card["chat_id"]), ("agent-two", b_chat))
        listed = self.service.list_approvals("user-one", "agent-one", self.chat)
        self.assertEqual([item["id"] for item in listed], [card["id"]])
        with self.assertRaises(ApprovalNotFound):  # A's chat cannot answer it: A has no say over B's tools
            self.service.resolve_approval("user-one", "agent-one", self.chat, card["id"], "approve")
        self.assertEqual(
            self.service.resolve_approval("user-one", "agent-two", b_chat, card["id"], "approve")["status"], "approved",
        )
        asker.join(3)
        self.assertTrue(outcome[0].approved)
        release.set()
        consumer.join(3)

    def _b_prompt_key(self, text: str) -> str:
        """The gate key: the responder matches the whole latest user message, which for B is the full mention prompt."""
        from src.service.mentions import chat_excerpt, mention_prompt
        return mention_prompt(text, "agent-one", "agent-one", chat_excerpt(self.sources[A].load(self.chat)))


if __name__ == "__main__":
    unittest.main()
