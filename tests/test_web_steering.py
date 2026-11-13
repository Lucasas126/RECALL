from __future__ import annotations

import json
import re
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

import test_service_steering as steering
from src.providers.cancellation import GenerationCancelled
from src.web.app import create_app
from auth_helpers import login


def _events(response) -> list[dict]:
    result = []
    for frame in response.text.strip().split("\n\n"):
        event = {}
        for line in frame.splitlines():
            name, value = line.split(": ", 1)
            event[name] = json.loads(value) if name == "data" else value
        result.append(event)
    return result


class WebSteeringTests(steering.SteeringFixture):
    def setUp(self) -> None:
        super().setUp()
        self.client = TestClient(create_app(self.service))
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        login(self.client, "user-one")
        self.messages_url = f"/api/agents/agent-one/chats/{self.chat}/messages"

    def test_concurrent_http_messages_interrupt_and_preserve_history(self) -> None:
        started, release = self._gate("Original")
        with ThreadPoolExecutor(max_workers=2) as pool:
            original = pool.submit(self.client.post, self.messages_url, json={"content": "Original"})
            self.assertTrue(started.wait(2))
            correction = pool.submit(
                self.client.post, self.messages_url, json={"content": "Correction"},
            )
            token = self.calls[("user-one", "agent-one")][0]["cancellation"]
            deadline = time.monotonic() + 2
            while True:
                try:
                    token.raise_if_cancelled()
                except GenerationCancelled:
                    break
                self.assertLess(time.monotonic(), deadline, "second HTTP request did not steer")
                time.sleep(0.01)
            release.set()
            old_response = original.result(timeout=3)
            new_response = correction.result(timeout=3)
        self.assertEqual(old_response.status_code, 200)
        self.assertEqual(new_response.status_code, 200)
        self.assertEqual(old_response.headers["x-accel-buffering"], "no")
        old, new = _events(old_response), _events(new_response)
        self.assertEqual([e["event"] for e in old], ["text", "interrupted", "done"])
        self.assertEqual(old[0]["data"], "Partial")
        self.assertEqual(old[1]["data"], {"content": "Partial", "reasoning": "", "actions": []})
        self.assertEqual([e["event"] for e in new], ["text", "usage", "done"])
        self.assertEqual(new[0]["data"], "Answer: Correction")
        self.assertTrue(old[0]["id"])
        self.assertTrue(all(e["id"] == old[0]["id"] for e in old))
        self.assertTrue(all(e["id"] == new[0]["id"] for e in new))
        self.assertNotEqual(old[0]["id"], new[0]["id"])
        history = self.client.get(f"/api/agents/agent-one/chats/{self.chat}").json()["messages"]
        self.assertTrue(history[1]["interrupted"])
        self.assertEqual(history[-1]["content"], "Answer: Correction")

    def test_stop_endpoint_cancels_the_running_reply_and_keeps_the_partial(self) -> None:
        started, release = self._gate("Original")
        with ThreadPoolExecutor(max_workers=1) as pool:
            original = pool.submit(self.client.post, self.messages_url, json={"content": "Original"})
            self.assertTrue(started.wait(2))
            stop = self.client.post(f"/api/agents/agent-one/chats/{self.chat}/stop")
            self.assertEqual(stop.status_code, 202)
            release.set()
            events = _events(original.result(timeout=3))
        self.assertEqual([e["event"] for e in events], ["text", "interrupted", "done"])
        history = self.client.get(f"/api/agents/agent-one/chats/{self.chat}").json()["messages"]
        self.assertTrue(history[-1]["interrupted"])

    def test_stop_endpoint_unknown_chat_and_nothing_running(self) -> None:
        self.assertEqual(self.client.post("/api/agents/agent-one/chats/nope/stop").status_code, 404)
        self.assertEqual(self.client.post(f"/api/agents/agent-one/chats/{self.chat}/stop").status_code, 202)

    def test_usage_command_is_answered_by_recall_and_not_stored(self) -> None:
        report = {"available": True, "windows": [
            {"key": "session", "percent": 6.4, "resets_at": None}, {"key": "week", "percent": 14.0, "resets_at": None},
        ]}
        with steering.patch.object(self.service, "claude_usage", return_value=report):
            events = _events(self.client.post(self.messages_url, json={"content": "/usage"}))
        self.assertEqual([e["event"] for e in events], ["text", "done"])
        self.assertIn("Session (5 h): 6%", events[0]["data"])
        self.assertIn("Week: 14%", events[0]["data"])
        history = self.client.get(f"/api/agents/agent-one/chats/{self.chat}").json()["messages"]
        self.assertEqual(history, [])

    def test_usage_command_without_login_explains_why(self) -> None:
        report = {"available": False, "reason": "no_own_login"}
        with steering.patch.object(self.service, "claude_usage", return_value=report):
            events = _events(self.client.post(self.messages_url, json={"content": "/usage"}))
        self.assertIn("no Claude login of your own", events[0]["data"])

    def test_stream_error_remains_explicit_and_followed_by_done(self) -> None:
        with self.assertLogs("recall", level="ERROR"):
            response = self.client.post(self.messages_url, json={"content": "fail"})
        events = _events(response)
        self.assertEqual([e["event"] for e in events], ["error", "done"])
        self.assertIn("provider unavailable", events[0]["data"])

    def test_associative_matches_are_sent_and_persisted(self) -> None:
        self.associative_memory = True
        provider = self.providers[("user-one", "agent-one")]
        original_invoke = provider.invoke.side_effect

        def delayed_model(*args, **kwargs):
            time.sleep(0.15)  # the hit lands while the model is still silent
            return original_invoke(*args, **kwargs)

        provider.invoke.side_effect = delayed_model

        def delayed_recall(*args, **kwargs):
            time.sleep(0.03)
            return "[associative memory] remembered detail", 2

        with steering.patch(
            "src.agent.agent.associative.recall",
            side_effect=delayed_recall,
        ):
            response = self.client.post(self.messages_url, json={"content": "Question"})

        events = _events(response)
        self.assertEqual([event["event"] for event in events], ["memory", "memory", "text", "usage", "done"])
        self.assertEqual(events[0]["data"], {"state": "searching"})
        self.assertEqual(events[1]["data"], {"state": "done", "matches": 2})
        report = self.agents[("user-one", "agent-one")].last_latency_report
        self.assertIn(report, events[3]["data"])
        total_match = re.search(r"latency: total=(\d+) ms", report)
        assoc_match = re.search(r"associative memory \(parallel\): (\d+) ms \(([\d.]+)%\)", report)
        model_match = re.search(r"model calls: (\d+) ms \(([\d.]+)%\)", report)
        self.assertIsNotNone(total_match)
        self.assertIsNotNone(assoc_match)
        self.assertIsNotNone(model_match)
        total_ms = int(total_match.group(1))
        assoc_ms, assoc_pct = int(assoc_match.group(1)), float(assoc_match.group(2))
        model_ms, model_pct = int(model_match.group(1)), float(model_match.group(2))
        self.assertGreaterEqual(assoc_ms, 20)
        self.assertGreaterEqual(model_ms, 20)
        self.assertAlmostEqual(assoc_pct, assoc_ms / total_ms * 100, delta=2)
        self.assertAlmostEqual(model_pct, model_ms / total_ms * 100, delta=2)
        self.assertIn("tools/other:", report)
        self.assertIn(
            "[associative memory] remembered detail",
            self.calls[("user-one", "agent-one")][-1]["context"][-1]["content"],
        )
        history = self.client.get(
            f"/api/agents/agent-one/chats/{self.chat}"
        ).json()["messages"]
        self.assertEqual(history[-1]["associative_matches"], 2)
        self.assertTrue(history[-1]["usage_report"].startswith("context "))
        self.assertNotIn("latency:", history[-1]["usage_report"])
        self.assertEqual(history[-1]["usage"], {"in": 10, "out": 5})

    def _slow_model(self) -> None:
        provider = self.providers[("user-one", "agent-one")]
        original_invoke = provider.invoke.side_effect

        def delayed_model(*args, **kwargs):
            time.sleep(0.1)
            return original_invoke(*args, **kwargs)

        provider.invoke.side_effect = delayed_model

    def _memory_events(self, response) -> list[dict]:
        return [e["data"] for e in _events(response) if e["event"] == "memory"]

    def _last_message(self) -> dict:
        return self.client.get(f"/api/agents/agent-one/chats/{self.chat}").json()["messages"][-1]

    def test_get_chat_returns_the_injected_fragments(self) -> None:
        from src.agent.associative import RecallResult

        self.associative_memory = True
        self._slow_model()
        fragment = {"address": "notes:cat.md", "label": "Cat", "score": 0.81, "excerpt": "The cat is called Misu."}
        result = RecallResult("[associative memory] remembered detail", 1, [fragment], "cat name")
        with steering.patch("src.agent.agent.associative.recall", return_value=result):
            response = self.client.post(self.messages_url, json={"content": "Question"})
        done = [e["data"] for e in _events(response) if e["event"] == "memory" and e["data"].get("state") == "done"]
        self.assertEqual(done, [{"state": "done", "matches": 1, "fragments": [fragment], "query": "cat name"}])
        message = self._last_message()
        self.assertEqual(message["associative_fragments"], [fragment])
        self.assertEqual(message["associative_query"], "cat name")

    def test_search_without_matches_is_sent_and_persisted(self) -> None:
        self.associative_memory = True
        self._slow_model()
        with steering.patch("src.agent.agent.associative.recall", return_value=(None, 0)):
            response = self.client.post(self.messages_url, json={"content": "Question"})
        self.assertEqual(self._memory_events(response), [
            {"state": "searching"}, {"state": "done", "matches": 0},
        ])
        self.assertEqual(self._last_message()["associative_matches"], 0)

    def test_failed_search_is_sent_as_skipped_and_persisted(self) -> None:
        self.associative_memory = True
        self._slow_model()
        with (
            steering.patch("src.agent.agent.associative.recall", side_effect=RuntimeError("index offline")),
            self.assertLogs("recall", level="ERROR"),
        ):
            response = self.client.post(self.messages_url, json={"content": "Question"})
        self.assertEqual(self._memory_events(response), [{"state": "searching"}, {"state": "skipped"}])
        message = self._last_message()
        self.assertTrue(message["associative_skipped"])
        self.assertNotIn("associative_matches", message)

    def test_validation_precedes_sse_response(self) -> None:
        response = self.client.post(self.messages_url, json={"content": " "})
        self.assertEqual(response.status_code, 400)
        self.assertIn("empty", response.json()["detail"])
        self.client.post("/api/logout")
        response = self.client.post(self.messages_url, json={"content": "Hello"})
        self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main()
