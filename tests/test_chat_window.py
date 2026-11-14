from __future__ import annotations

import unittest

import test_service_steering as steering
from auth_helpers import login
from src.web.app import create_app
from fastapi.testclient import TestClient


class ChatWindowTests(steering.SteeringFixture):
    """`get_chat(limit=, before=)`: the end of a long chat first, older messages on demand."""

    def setUp(self) -> None:
        super().setUp()
        self.conversations = self.sources[("user-one", "agent-one")]
        for number in range(1, 11):  # 10 exchanges: stored indexes 0..19, user turns at even positions
            self.conversations.append(self.chat, "user", f"question {number}", ts=f"2026-11-14T10:{number:02d}:00+01:00")
            self.conversations.append(self.chat, "assistant", f"answer {number}", ts=f"2026-11-14T10:{number:02d}:05+01:00")

    def get(self, **kwargs) -> dict:
        return self.service.get_chat("user-one", "agent-one", self.chat, **kwargs)

    def texts(self, data: dict) -> list[str]:
        return [message["content"] for message in data["messages"]]

    def test_without_a_limit_everything_comes_back_and_nothing_is_hidden(self) -> None:
        data = self.get()
        self.assertEqual(len(data["messages"]), 20)
        self.assertEqual(data["window"], {"total": 20, "hidden": 0, "first_index": 0})

    def test_a_limit_returns_the_last_messages_and_counts_the_hidden_ones(self) -> None:
        data = self.get(limit=6)
        self.assertEqual(self.texts(data), ["question 8", "answer 8", "question 9", "answer 9", "question 10", "answer 10"])
        self.assertEqual(data["window"], {"total": 20, "hidden": 14, "first_index": 14})
        self.assertEqual([m["index"] for m in data["messages"]], [14, 15, 16, 17, 18, 19])  # stored indexes survive (edits use them)

    def test_before_pages_backwards_until_the_start(self) -> None:
        first = self.get(limit=6)
        second = self.get(limit=6, before=first["window"]["first_index"])
        self.assertEqual(self.texts(second), ["question 5", "answer 5", "question 6", "answer 6", "question 7", "answer 7"])
        self.assertEqual(second["window"], {"total": 20, "hidden": 8, "first_index": 8})
        third = self.get(limit=20, before=second["window"]["first_index"])
        self.assertEqual(len(third["messages"]), 8)
        self.assertEqual(third["window"], {"total": 20, "hidden": 0, "first_index": 0})
        seen = self.texts(third) + self.texts(second) + self.texts(first)
        self.assertEqual(seen, self.texts(self.get()))  # the pages tile the chat exactly: nothing lost, nothing twice

    def test_before_alone_returns_everything_before_it(self) -> None:
        data = self.get(before=4)
        self.assertEqual(self.texts(data), ["question 1", "answer 1", "question 2", "answer 2"])
        self.assertEqual(data["window"]["hidden"], 0)

    def test_a_limit_larger_than_the_chat_and_an_empty_window(self) -> None:
        self.assertEqual(len(self.get(limit=500)["messages"]), 20)
        data = self.get(limit=5, before=0)
        self.assertEqual((data["messages"], data["window"]), ([], {"total": 20, "hidden": 0, "first_index": None}))

    def test_tool_turns_do_not_count_but_keep_their_place_in_the_indexes(self) -> None:
        chat = self.service.create_chat("user-one", "agent-one")["id"]
        self.conversations.append(chat, "user", "a")
        self.conversations.append(chat, "tool", "tool output")
        self.conversations.append(chat, "assistant", "b")
        self.conversations.append(chat, "user", "c")
        data = self.service.get_chat("user-one", "agent-one", chat, limit=2)
        self.assertEqual([(m["content"], m["index"]) for m in data["messages"]], [("b", 2), ("c", 3)])
        self.assertEqual(data["window"], {"total": 3, "hidden": 1, "first_index": 2})

    def test_compaction_dividers_travel_with_their_window_but_are_not_counted(self) -> None:
        chat = self.service.create_chat("user-one", "agent-one")["id"]
        self.conversations.append(chat, "user", "old question")
        self.conversations.append(chat, "assistant", "old answer")
        self.conversations.append(chat, "compaction", "summary", tokens_before=9000, tokens_after=900)
        self.conversations.append(chat, "user", "new question")
        data = self.service.get_chat("user-one", "agent-one", chat, limit=2)
        self.assertEqual([m["role"] for m in data["messages"]], ["compaction", "user"])
        self.assertEqual(data["window"]["total"], 3)
        self.assertEqual(data["window"]["hidden"], 2)

    def test_a_running_replys_checkpoint_stays_out_of_every_window(self) -> None:
        started, release = self._gate("Original")
        original = self._send("Original")
        self.assertTrue(started.wait(2))
        self.conversations.checkpoint(self.chat, "Working on it", replace=True)
        data = self.get(limit=2)
        self.assertNotIn("Working on it", self.texts(data))
        self.assertEqual(self.texts(data)[-1], "Original")
        release.set()
        list(original)

    def test_the_rest_of_the_answer_is_the_same_with_and_without_a_window(self) -> None:
        full, windowed = self.get(), self.get(limit=2)
        for key in ("chat", "model", "model_source", "reasoning_effort"):
            self.assertEqual(full[key], windowed[key], key)

    def test_the_worker_chat_of_a_task_is_read_whole(self) -> None:
        import inspect

        self.assertNotIn("limit", inspect.getsource(self.service.get_task_chat))


class ChatWindowApiTests(steering.SteeringFixture):
    def setUp(self) -> None:
        super().setUp()
        conversations = self.sources[("user-one", "agent-one")]
        for number in range(1, 6):
            conversations.append(self.chat, "user", f"q{number}")
            conversations.append(self.chat, "assistant", f"a{number}")
        self.client = TestClient(create_app(self.service))
        login(self.client, "user-one")
        self.url = f"/api/agents/agent-one/chats/{self.chat}"

    def test_the_query_parameters_reach_the_service(self) -> None:
        data = self.client.get(self.url, params={"limit": 3}).json()
        self.assertEqual([m["content"] for m in data["messages"]], ["a4", "q5", "a5"])
        self.assertEqual(data["window"], {"total": 10, "hidden": 7, "first_index": 7})
        older = self.client.get(self.url, params={"limit": 3, "before": 7}).json()
        self.assertEqual([m["content"] for m in older["messages"]], ["q3", "a3", "q4"])
        self.assertEqual(len(self.client.get(self.url).json()["messages"]), 10)

    def test_nonsense_is_refused_not_guessed(self) -> None:
        for params in ({"limit": 0}, {"limit": -1}, {"limit": 99999}, {"limit": "x"}, {"before": -2}):
            with self.subTest(params=params):
                self.assertEqual(self.client.get(self.url, params=params).status_code, 422)

    def test_another_users_chat_is_still_not_reachable(self) -> None:
        other = TestClient(create_app(self.service))
        login(other, "user-two")
        self.assertEqual(other.get(self.url, params={"limit": 3}).status_code, 404)


if __name__ == "__main__":
    unittest.main()
