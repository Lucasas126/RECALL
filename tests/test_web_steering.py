from __future__ import annotations

import json
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

from fastapi.testclient import TestClient

import test_service_steering as steering
from src.providers.cancellation import GenerationCancelled
from src.web.app import create_app


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
        login = self.client.post("/api/login", json={"user_id": "user-one"})
        self.assertEqual(login.status_code, 200)
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

    def test_stream_error_remains_explicit_and_followed_by_done(self) -> None:
        with self.assertLogs("recall", level="ERROR"):
            response = self.client.post(self.messages_url, json={"content": "fail"})
        events = _events(response)
        self.assertEqual([e["event"] for e in events], ["error", "done"])
        self.assertIn("provider unavailable", events[0]["data"])

    def test_validation_precedes_sse_response(self) -> None:
        response = self.client.post(self.messages_url, json={"content": " "})
        self.assertEqual(response.status_code, 400)
        self.assertIn("empty", response.json()["detail"])
        self.client.post("/api/logout")
        response = self.client.post(self.messages_url, json={"content": "Hello"})
        self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main()
