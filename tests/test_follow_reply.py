from __future__ import annotations

import threading
import time
import unittest

from fastapi.testclient import TestClient

import test_service_steering as steering
import test_terminal_web as terminal_web
from auth_helpers import login
from src.service.chat import _REPLAY_MAX_EVENTS, ChatEvent, UnknownChat, _RunFeed
from src.terminal.context import current_run
from src.web.app import create_app


class RunFeedTests(unittest.TestCase):
    def drain(self, queue) -> list:
        items = []
        while True:
            item = queue.get(timeout=1)
            if item is None:
                return items
            items.append(item)

    def test_late_subscriber_gets_history_then_live_events_in_order(self) -> None:
        feed = _RunFeed()
        first = feed.subscribe()
        feed.put(ChatEvent("reasoning", "a", generation_id="g"))
        feed.put(ChatEvent("text", "b", generation_id="g"))
        late = feed.subscribe()
        feed.put(ChatEvent("text", "c", generation_id="g"))
        feed.put(None)
        self.assertEqual([e.text for e in self.drain(first)], ["a", "b", "c"])
        self.assertEqual([(e.kind, e.text) for e in self.drain(late)], [("reasoning", "a"), ("text", "b"), ("text", "c")])

    def test_history_merges_chunks_and_is_bounded(self) -> None:
        feed = _RunFeed()
        for _ in range(100):
            feed.put(ChatEvent("text", "x", generation_id="g"))
        self.assertEqual(self.drain_after_close(feed)[0].text, "x" * 100)
        feed = _RunFeed()
        for index in range(_REPLAY_MAX_EVENTS + 10):
            feed.put(ChatEvent("tool", data={"n": index}, generation_id="g"))
        replay = self.drain_after_close(feed)
        self.assertEqual(len(replay), _REPLAY_MAX_EVENTS)
        self.assertEqual(replay[-1].data, {"n": _REPLAY_MAX_EVENTS + 9})

    def drain_after_close(self, feed: _RunFeed) -> list:
        feed.put(None)
        return self.drain(feed.subscribe())

    def test_closed_feed_still_replays_and_unsubscribed_queue_gets_nothing(self) -> None:
        feed = _RunFeed()
        queue = feed.subscribe()
        feed.unsubscribe(queue)
        feed.put(ChatEvent("text", "t", generation_id="g"))
        self.assertTrue(queue.empty())

    def test_exception_is_replayed(self) -> None:
        feed = _RunFeed()
        feed.put(RuntimeError("boom"))
        feed.put(None)
        self.assertIsInstance(self.drain(feed.subscribe())[0], RuntimeError)


class FollowServiceTests(steering.SteeringFixture):
    def follow(self):
        return self.service.follow("user-one", "agent-one", self.chat)

    def test_second_viewer_replays_then_follows_without_stealing(self) -> None:
        started, release = self._gate("Original")
        original = self._send("Original")
        self.assertTrue(started.wait(2))
        self.assertTrue(self.service.chat_active("user-one", "agent-one", self.chat))
        viewer = self.follow()
        first = next(viewer)
        self.assertEqual((first.kind, first.text), ("text", "Partial"))
        release.set()
        seen = [first, *viewer]
        original_events = list(original)
        self.assertEqual([(e.kind, e.text) for e in original_events], [(e.kind, e.text) for e in seen])
        self.assertEqual([e.kind for e in seen], ["text", "text", "usage"])
        self.assertEqual({e.generation_id for e in seen}, {original_events[0].generation_id})

    def test_dropping_a_viewer_does_not_cancel_the_reply(self) -> None:
        started, release = self._gate("Original")
        original = self._send("Original")
        self.assertTrue(started.wait(2))
        viewer = self.follow()
        next(viewer)
        viewer.close()
        release.set()
        self.assertEqual([e.kind for e in original], ["text", "text", "usage"])
        history = self.service.get_chat("user-one", "agent-one", self.chat)["messages"]
        self.assertEqual(history[-1]["content"], "PartialAnswer: Original")
        self.assertFalse(history[-1].get("interrupted", False))

    def test_dropping_the_posting_stream_keeps_the_reply_running(self) -> None:
        started, release = self._gate("Original")
        original = self._send("Original")
        self.assertTrue(started.wait(2))
        original.close()  # the web request went away
        viewer = self.follow()
        release.set()
        self.assertEqual([e.kind for e in viewer], ["text", "text", "usage"])

    def test_no_active_run_returns_none_and_buffer_is_dropped(self) -> None:
        self.assertIsNone(self.follow())
        events = list(self._send("Hello"))
        self.assertEqual(events[-1].kind, "usage")
        deadline = time.monotonic() + 2
        while self.service.chat_active("user-one", "agent-one", self.chat):
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.01)
        self.assertIsNone(self.follow())

    def test_unknown_chat_and_agent(self) -> None:
        with self.assertRaises(UnknownChat):
            self.service.follow("user-one", "agent-one", "nope")

    def test_active_flag_in_chat_data_and_list(self) -> None:
        started, release = self._gate("Original")
        original = self._send("Original")
        self.assertTrue(started.wait(2))
        self.assertTrue(self.service.get_chat("user-one", "agent-one", self.chat)["chat"]["active"])
        listed = {c["id"]: c for c in self.service.list_chats("user-one", "agent-one")}
        self.assertTrue(listed[self.chat]["active"])
        release.set()
        list(original)


class FollowApiTests(steering.SteeringFixture):
    def setUp(self) -> None:
        super().setUp()
        provider = self.providers[("user-one", "agent-one")]
        normal = provider.invoke.side_effect

        def asking(messages, model, **kwargs):
            latest = next(m["content"] for m in reversed(messages) if m["role"] == "user")
            if "ask" not in str(latest):
                yield from normal(messages, model, **kwargs)
                return
            run = current_run()
            outcome = run.broker.request(
                run.user_id, run.agent_id, run.chat_id, run, "terminal", "ls -la", "~/workspace", "needs a look",
                "manual", timeout_seconds=5,
            )
            yield "content", f"outcome: {outcome.status}"

        provider.invoke.side_effect = asking
        self.client = TestClient(create_app(self.service))
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        login(self.client, "user-one")
        self.base = f"/api/agents/agent-one/chats/{self.chat}"
        self.other = TestClient(create_app(self.service))
        self.other.__enter__()
        self.addCleanup(self.other.__exit__, None, None, None)
        login(self.other, "user-two")

    def test_no_active_run_is_204_and_unknown_chat_404(self) -> None:
        response = self.client.get(f"{self.base}/stream")
        self.assertEqual((response.status_code, response.content), (204, b""))
        self.assertEqual(self.client.get("/api/agents/agent-one/chats/nope/stream").status_code, 404)
        self.assertEqual(self.other.get(f"{self.base}/stream").status_code, 404)
        self.assertEqual(TestClient(create_app(self.service)).get(f"{self.base}/stream").status_code, 401)

    def test_follower_gets_replay_and_live_rest_as_sse(self) -> None:
        started, release = self._gate("Original")
        post: dict = {}
        thread = threading.Thread(
            target=lambda: post.update(r=self.client.post(f"{self.base}/messages", json={"content": "Original"})), daemon=True,
        )
        thread.start()
        self.addCleanup(thread.join, 5)
        self.assertTrue(started.wait(2))
        threading.Timer(0.3, release.set).start()
        response = self.client.get(f"{self.base}/stream")
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
        events = terminal_web._events(response)
        self.assertEqual([e["event"] for e in events], ["text", "text", "usage", "done"])
        self.assertEqual(events[0]["data"], "Partial")
        thread.join(5)
        self.assertEqual([e["event"] for e in terminal_web._events(post["r"])], [e["event"] for e in events])
        self.assertEqual(events[0]["id"], terminal_web._events(post["r"])[0]["id"])

    def test_approval_is_visible_to_a_reconnecting_viewer(self) -> None:
        post: dict = {}
        thread = threading.Thread(
            target=lambda: post.update(r=self.client.post(f"{self.base}/messages", json={"content": "please ask"})), daemon=True,
        )
        thread.start()
        self.addCleanup(thread.join, 5)
        deadline = time.monotonic() + 3
        while not (pending := self.client.get(f"{self.base}/approvals").json()):
            self.assertLess(time.monotonic(), deadline)
            time.sleep(0.02)
        viewer = self.service.follow("user-one", "agent-one", self.chat)
        replayed = next(viewer)
        self.assertEqual((replayed.kind, replayed.data["id"]), ("approval", pending[0]["id"]))
        answer = self.client.post(f"{self.base}/approvals/{pending[0]['id']}", json={"decision": "approve"})
        self.assertEqual(answer.json(), {"status": "approved"})
        rest = [e.kind for e in viewer]
        self.assertEqual(rest, ["approval_resolved", "text", "usage"])
        thread.join(5)
        self.assertEqual(self.client.get(f"{self.base}/approvals").json(), [])


if __name__ == "__main__":
    unittest.main()
