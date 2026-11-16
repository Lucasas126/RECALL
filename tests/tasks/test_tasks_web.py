"""Phase 2b of the subagents feature: the web API of tasks (list, detail, steer, stop, read-only worker chat)."""

from __future__ import annotations

import threading
import unittest
from unittest import mock

from fastapi.testclient import TestClient

from tests.tasks import test_subagents_async as base
from tests.support.auth_helpers import login
from src.web.app import create_app

USER, AGENT = base.USER, base.AGENT


class TasksWebTests(base.AsyncFixture):
    def setUp(self) -> None:
        super().setUp()
        self.client = TestClient(create_app(self.service))
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        login(self.client, USER)
        self.child_behavior = "slow"

    def finish(self, live) -> None:
        self.release_child.set()
        self.assertTrue(base.until(lambda: live.done.is_set()))

    def test_requires_a_session(self) -> None:
        anonymous = TestClient(create_app(self.service))
        self.assertEqual(anonymous.get("/api/tasks").status_code, 401)
        self.assertEqual(anonymous.post("/api/tasks/x/stop").status_code, 401)

    def test_list_for_the_parent_chat_shows_a_running_task(self) -> None:
        live = self.start()
        data = self.client.get("/api/tasks", params={"agent": AGENT, "chat": self.chat}).json()
        self.assertEqual([t["id"] for t in data["tasks"]], [live.task_id])
        task = data["tasks"][0]
        self.assertEqual(task["status"], "running")
        self.assertEqual(task["target"]["kind"], "ephemeral")
        self.assertEqual(task["parent"]["chat_id"], self.chat)
        self.assertTrue(task["child"]["chat_id"])
        self.assertIsInstance(task["elapsed_s"], int)
        self.assertFalse(data["pending_wake"])
        self.assertIn("active", data)
        self.finish(live)

    def test_list_filters_by_chat_and_a_finished_task_has_a_result_preview(self) -> None:
        self.child_behavior = "ok"
        live = self.launch(wake=False)
        self.assertTrue(base.until(lambda: live.done.is_set()))
        other = self.service.create_chat(USER, AGENT)["id"]
        mine = self.client.get("/api/tasks", params={"agent": AGENT, "chat": self.chat}).json()["tasks"]
        self.assertEqual(mine[0]["status"], "done")
        self.assertIn("RESULT for: find X", mine[0]["result_preview"])
        self.assertIsNotNone(mine[0]["ended_at"])
        self.assertEqual(self.client.get("/api/tasks", params={"agent": AGENT, "chat": other}).json()["tasks"], [])
        self.assertEqual(len(self.client.get("/api/tasks").json()["tasks"]), 1)

    def test_detail_carries_the_full_result(self) -> None:
        self.child_behavior = "ok"
        live = self.launch(wake=False)
        self.assertTrue(base.until(lambda: live.done.is_set()))
        body = self.client.get(f"/api/tasks/{live.task_id}").json()
        self.assertEqual(body["goal"], "find X")
        self.assertIn("RESULT for: find X", body["result"])

    def test_pending_wake_is_reported_while_a_result_is_on_its_way(self) -> None:
        live = self.launch(wake=False)
        self.finish(live)
        self.manager.store.update(USER, live.task_id, lambda r: r.__setitem__("wake", {"state": "pending"}), final_ok=True)
        data = self.client.get("/api/tasks", params={"agent": AGENT, "chat": self.chat}).json()
        self.assertTrue(data["pending_wake"])

    def test_another_user_cannot_see_or_touch_a_task(self) -> None:
        live = self.start()
        other = TestClient(create_app(self.service))
        other.__enter__()
        self.addCleanup(other.__exit__, None, None, None)
        login(other, "user-two")
        self.assertEqual(other.get("/api/tasks").json()["tasks"], [])
        for method, url, kwargs in (
            ("get", f"/api/tasks/{live.task_id}", {}),
            ("get", f"/api/tasks/{live.task_id}/chat", {}),
            ("post", f"/api/tasks/{live.task_id}/stop", {}),
            ("post", f"/api/tasks/{live.task_id}/steer", {"json": {"text": "hi"}}),
        ):
            with self.subTest(url=url):
                self.assertEqual(getattr(other, method)(url, **kwargs).status_code, 404)
        self.assertEqual(self.manager.store.get(USER, live.task_id)["status"], "running")
        self.finish(live)

    def test_unknown_task_is_404(self) -> None:
        self.assertEqual(self.client.get("/api/tasks/nope").status_code, 404)
        self.assertEqual(self.client.post("/api/tasks/nope/stop").status_code, 404)

    def test_the_agent_filter_is_owner_checked(self) -> None:
        self.assertEqual(self.client.get("/api/tasks", params={"agent": "not-mine", "chat": "x"}).status_code, 404)

    def test_steer_corrects_a_running_task(self) -> None:
        live = self.start()
        response = self.client.post(f"/api/tasks/{live.task_id}/steer", json={"text": "use the fast path"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "running")
        self.release_child.set()
        child = self.manager.store.get(USER, live.task_id)["child"]
        self.assertTrue(base.until(lambda: "CORRECTED: use the fast path" in str(
            self.service._conversations(USER, child["agent_id"]).load(child["chat_id"]))))

    def test_steer_validates_text_and_state(self) -> None:
        live = self.start()
        self.assertEqual(self.client.post(f"/api/tasks/{live.task_id}/steer", json={"text": "  "}).status_code, 400)
        self.assertEqual(self.client.post(f"/api/tasks/{live.task_id}/steer", json={}).status_code, 422)
        self.finish(live)
        self.assertTrue(base.until(lambda: self.manager.store.get(USER, live.task_id)["status"] == "done"))
        self.assertEqual(self.client.post(f"/api/tasks/{live.task_id}/steer", json={"text": "late"}).status_code, 409)

    def test_stop_ends_a_running_task_and_is_idempotent(self) -> None:
        live = self.launch(wake=False)
        response = self.client.post(f"/api/tasks/{live.task_id}/stop")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "cancelled")
        again = self.client.post(f"/api/tasks/{live.task_id}/stop")
        self.assertEqual(again.status_code, 200)
        self.assertEqual(again.json()["status"], "cancelled")

    def test_worker_chat_is_readable_in_the_shape_of_get_chat_and_marked_internal(self) -> None:
        self.child_behavior = "ok"
        live = self.launch(wake=False)
        self.assertTrue(base.until(lambda: live.done.is_set()))
        body = self.client.get(f"/api/tasks/{live.task_id}/chat").json()
        self.assertTrue(body["task"]["internal"])
        self.assertEqual(body["task"]["id"], live.task_id)
        self.assertEqual(body["messages"][0]["role"], "user")
        self.assertIn("find X", body["messages"][0]["content"])
        self.assertEqual(body["messages"][-1]["role"], "assistant")
        self.assertIn("chat", body)
        # read-only: there is no write route on the task, and the hidden chat is not in the chat list
        listed = self.client.get(f"/api/agents/{AGENT}/chats").json()
        self.assertNotIn(body["task"]["chat_id"], [c["id"] for c in listed])
        self.assertEqual(self.client.post(f"/api/tasks/{live.task_id}/chat", json={"content": "x"}).status_code, 405)

    def test_stream_follows_a_running_worker_live_and_ends_with_it(self) -> None:
        live = self.start()
        result: dict = {}

        def read() -> None:
            result["response"] = self.client.get(f"/api/tasks/{live.task_id}/stream")

        reader = threading.Thread(target=read)
        reader.start()
        # the activity of the card comes from the same worker events
        self.assertTrue(base.until(lambda: (self.client.get(f"/api/tasks/{live.task_id}").json()["activity"] or {}).get("kind") == "text"))
        listed = self.client.get("/api/tasks", params={"agent": AGENT, "chat": self.chat}).json()["tasks"][0]
        self.assertEqual(listed["activity"]["kind"], "text")
        self.release_child.set()
        reader.join(10)
        self.assertFalse(reader.is_alive())
        response = result["response"]
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/event-stream"))
        self.assertIn("event: text", response.text)
        self.assertIn("RESULT for: find X", response.text)
        self.assertTrue(response.text.rstrip().endswith("event: done\ndata: \"\""))
        self.assertTrue(base.until(lambda: live.done.is_set()))

    def test_stream_of_an_idle_task_ends_at_once(self) -> None:
        self.child_behavior = "ok"
        live = self.launch(wake=False)
        self.assertTrue(base.until(lambda: live.done.is_set()))
        response = self.client.get(f"/api/tasks/{live.task_id}/stream")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, 'event: done\ndata: ""\n\n')
        self.assertIsNone(self.client.get(f"/api/tasks/{live.task_id}").json()["activity"])

    def test_stream_of_a_task_run_by_another_process_says_so(self) -> None:
        live = self.start()
        with mock.patch.object(type(self.manager), "runs_elsewhere", return_value=True):
            response = self.client.get(f"/api/tasks/{live.task_id}/stream")
        self.assertIn("event: unavailable", response.text)
        self.assertIn("event: done", response.text)
        self.assertNotIn("event: text", response.text)
        self.finish(live)

    def test_stream_is_owner_checked(self) -> None:
        live = self.start()
        other = TestClient(create_app(self.service))
        other.__enter__()
        self.addCleanup(other.__exit__, None, None, None)
        login(other, "user-two")
        self.assertEqual(other.get(f"/api/tasks/{live.task_id}/stream").status_code, 404)
        self.assertEqual(self.client.get("/api/tasks/nope/stream").status_code, 404)
        self.finish(live)

    def test_a_task_activity_names_the_tool_running(self) -> None:
        from types import SimpleNamespace
        from src.service.tasks import activity_of

        def event(kind, data=None):
            return SimpleNamespace(kind=kind, data=data, text="")

        start = event("tool_start", {"id": "1", "name": "terminal", "arguments": {"command": "ls -la\nsecond line"}})
        self.assertEqual(activity_of(start), {"kind": "tool", "tool": "terminal", "detail": "ls -la"})
        self.assertEqual(activity_of(event("reasoning"))["kind"], "reasoning")
        self.assertEqual(activity_of(event("text"))["kind"], "text")
        self.assertEqual(activity_of(event("usage"), {"kind": "text"}), {"kind": "text"})
        long = event("tool_start", {"name": "web_search", "arguments": '{"query": "' + "x" * 200 + '"}'})
        self.assertTrue(activity_of(long)["detail"].endswith("…"))
        self.assertLessEqual(len(activity_of(long)["detail"]), 80)

    def test_a_wake_turn_is_returned_with_source_and_task_ids(self) -> None:
        self.child_behavior = "ok"
        live = self.start()
        self.assertTrue(base.until(lambda: self.settled(1)))
        messages = self.client.get(f"/api/agents/{AGENT}/chats/{self.chat}").json()["messages"]
        woke = [m for m in messages if m.get("source") == "task"]
        self.assertEqual(woke[0]["task_ids"], [live.task_id])


if __name__ == "__main__":
    unittest.main()
