from __future__ import annotations

import json
import threading
import time
import unittest

from fastapi.testclient import TestClient

import test_service_steering as steering
from auth_helpers import login
from terminal_helpers import ssh_config
from src.terminal.context import current_run
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


class ApprovalApiTests(steering.SteeringFixture):
    def setUp(self) -> None:
        super().setUp()
        provider = self.providers[("user-one", "agent-one")]
        normal = provider.invoke.side_effect

        def asking(messages, model, **kwargs):
            latest = next(m["content"] for m in reversed(messages) if m["role"] == "user")
            if "ask" not in str(latest):
                yield from normal(messages, model, **kwargs)
                return
            run = current_run()  # the chat run the service bound around this reply
            outcome = run.broker.request(
                run.user_id, run.agent_id, run.chat_id, run, "terminal", "ls -la", "~/workspace", "needs a look",
                "manual", timeout_seconds=5,
            )
            yield "content", f"outcome: {outcome.status} ({outcome.reason})"

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

    def ask(self):
        """Start a reply that waits for an approval; returns (holder with the response, the pending approval)."""
        holder: dict = {}
        thread = threading.Thread(
            target=lambda: holder.update(response=self.client.post(f"{self.base}/messages", json={"content": "please ask"})),
            daemon=True,
        )
        thread.start()
        self.addCleanup(thread.join, 5)
        deadline = time.monotonic() + 3
        while True:
            pending = self.client.get(f"{self.base}/approvals").json()
            if pending:
                return holder, thread, pending[0]
            self.assertLess(time.monotonic(), deadline, "no approval became pending")
            time.sleep(0.02)

    def test_get_pending_lists_the_approval_payload(self) -> None:
        self.assertEqual(self.client.get(f"{self.base}/approvals").json(), [])
        holder, thread, pending = self.ask()
        for key in ("id", "tool", "command", "cwd", "reason", "risk", "timeout_seconds", "expires_at", "chat_id", "agent_id"):
            self.assertIn(key, pending)
        self.assertEqual((pending["tool"], pending["command"], pending["risk"], pending["chat_id"], pending["agent_id"]),
                         ("terminal", "ls -la", "manual", self.chat, "agent-one"))
        self.client.post(f"{self.base}/approvals/{pending['id']}", json={"decision": "deny"})
        thread.join(5)

    def test_approve_resumes_the_reply_and_the_stream_carries_both_events(self) -> None:
        holder, thread, pending = self.ask()
        answer = self.client.post(f"{self.base}/approvals/{pending['id']}", json={"decision": "approve"})
        self.assertEqual((answer.status_code, answer.json()), (200, {"status": "approved", "scope": "once"}))
        thread.join(5)
        events = _events(holder["response"])
        kinds = [e["event"] for e in events]
        self.assertLess(kinds.index("approval"), kinds.index("approval_resolved"))
        self.assertEqual(events[kinds.index("approval")]["data"]["id"], pending["id"])
        self.assertEqual(events[kinds.index("approval_resolved")]["data"],
                         {"id": pending["id"], "decision": "approved", "reason": "approved", "scope": "once"})
        self.assertIn("outcome: approved", [e["data"] for e in events if e["event"] == "text"][-1])
        self.assertEqual(kinds[-1], "done")
        self.assertEqual(self.client.get(f"{self.base}/approvals").json(), [])

    def test_deny(self) -> None:
        holder, thread, pending = self.ask()
        answer = self.client.post(f"{self.base}/approvals/{pending['id']}", json={"decision": "deny"})
        self.assertEqual((answer.status_code, answer.json()), (200, {"status": "denied", "scope": "deny"}))
        thread.join(5)
        texts = [e["data"] for e in _events(holder["response"]) if e["event"] == "text"]
        self.assertIn("outcome: denied (denied by user)", texts[-1])

    def test_answering_twice_is_safe(self) -> None:
        holder, thread, pending = self.ask()
        url = f"{self.base}/approvals/{pending['id']}"
        self.assertEqual(self.client.post(url, json={"decision": "approve"}).json(), {"status": "approved", "scope": "once"})
        self.assertEqual(self.client.post(url, json={"decision": "approve"}).json(), {"status": "approved", "scope": "once"})
        thread.join(5)

    def test_unknown_approval_is_404(self) -> None:
        response = self.client.post(f"{self.base}/approvals/nope", json={"decision": "approve"})
        self.assertEqual(response.status_code, 404)

    def test_another_user_gets_403_and_the_approval_stays_pending(self) -> None:
        holder, thread, pending = self.ask()
        response = self.other.post(f"{self.base}/approvals/{pending['id']}", json={"decision": "approve"})
        self.assertEqual(response.status_code, 403)
        self.assertEqual(len(self.client.get(f"{self.base}/approvals").json()), 1)
        self.assertEqual(self.other.get(f"{self.base}/approvals").status_code, 404)  # not their chat: nothing is listed
        self.client.post(f"{self.base}/approvals/{pending['id']}", json={"decision": "deny"})
        thread.join(5)

    def test_bad_decision_is_422(self) -> None:
        holder, thread, pending = self.ask()
        url = f"{self.base}/approvals/{pending['id']}"
        for body in ({"decision": "maybe"}, {}, {"decision": "APPROVE"}):
            self.assertEqual(self.client.post(url, json=body).status_code, 422, body)
        self.client.post(url, json={"decision": "deny"})
        thread.join(5)

    def test_unknown_chat_and_anonymous_requests(self) -> None:
        self.assertEqual(self.client.get("/api/agents/agent-one/chats/missing/approvals").status_code, 404)
        anonymous = TestClient(create_app(self.service))
        self.assertEqual(anonymous.get(f"{self.base}/approvals").status_code, 401)


class TerminalBackendListingTests(steering.SteeringFixture):
    BACKEND = ssh_config(host="203.0.113.77", user="sandboxer", port=2222, key_file="/keys/secret_id", known_hosts_file="/keys/kh_secret",
                         description="User A's PC, real files")
    backends: dict = {}

    def _user(self, user_id):
        user = super()._user(user_id)
        return user.model_copy(update={"terminal_backends": dict(self.backends)})

    def me(self):
        client = TestClient(create_app(self.service))
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        login(client, "user-one")
        return client.get("/api/me")

    def test_no_backends_by_default(self) -> None:
        self.assertEqual(self.me().json()["terminal_backends"], [])

    def test_lists_only_name_type_and_description(self) -> None:
        self.backends = {"box": self.BACKEND, "alpha": self.BACKEND.model_copy(update={"description": ""})}
        response = self.me()
        self.assertEqual(response.json()["terminal_backends"], [
            {"name": "alpha", "type": "ssh", "description": ""},
            {"name": "box", "type": "ssh", "description": "User A's PC, real files"},
        ])
        for secret in ("203.0.113.77", "sandboxer", "2222", "/keys/secret_id", "secret_id", "kh_secret"):
            self.assertNotIn(secret, response.text)


if __name__ == "__main__":
    unittest.main()
