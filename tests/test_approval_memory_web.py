"""Approval memory over the web API: the card payload carries `options`, the answer carries `scope`."""

from __future__ import annotations

import json
import os
import sys
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))
os.environ.setdefault("RECALL_SESSION_SECRET", "x")

import test_service_steering as steering
from auth_helpers import login
from fastapi.testclient import TestClient

from src.terminal.approvals import Learn
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


class _ScopedFixture(steering.SteeringFixture):
    options: tuple[str, ...] = ("once", "session")

    def setUp(self) -> None:
        super().setUp()
        provider = self.providers[("user-one", "agent-one")]
        normal = provider.invoke.side_effect
        self.persisted: list | object = []
        self.outcomes: list = []

        def asking(messages, model, **kwargs):
            latest = next(m["content"] for m in reversed(messages) if m["role"] == "user")
            if "ask" not in str(latest):
                yield from normal(messages, model, **kwargs)
                return
            run = current_run()
            persist = lambda rules: self.persisted.append(rules)  # noqa: E731
            learn = Learn("sig", ["rule"], persist, None) if "agent" in self.options else Learn("sig")
            outcome = run.broker.request(
                run.user_id, run.agent_id, run.chat_id, run, "terminal", "ls -la", "~/workspace", "needs a look", "manual",
                timeout_seconds=5, options=self.options, learn=learn,
            )
            self.outcomes.append(outcome)
            yield "content", f"outcome: {outcome.status} {outcome.scope}"

        provider.invoke.side_effect = asking
        self.client = TestClient(create_app(self.service))
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        login(self.client, "user-one")
        self.base = f"/api/agents/agent-one/chats/{self.chat}"

    def ask(self):
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



class ScopedApprovalApiTests(_ScopedFixture):
    def test_the_pending_payload_and_the_stream_carry_the_options(self) -> None:
        holder, thread, pending = self.ask()
        self.assertEqual(pending["options"], ["once", "session"])
        self.client.post(f"{self.base}/approvals/{pending['id']}", json={"decision": "approve", "scope": "session"})
        thread.join(5)
        events = _events(holder["response"])
        card = next(e["data"] for e in events if e["event"] == "approval")
        self.assertEqual(card["options"], ["once", "session"])
        resolved = next(e["data"] for e in events if e["event"] == "approval_resolved")
        self.assertEqual((resolved["decision"], resolved["scope"]), ("approved", "session"))

    def test_session_scope_is_granted_and_remembered_for_the_chat(self) -> None:
        holder, thread, pending = self.ask()
        answer = self.client.post(f"{self.base}/approvals/{pending['id']}", json={"decision": "approve", "scope": "session"})
        self.assertEqual(answer.json(), {"status": "approved", "scope": "session"})
        thread.join(5)
        self.assertTrue(self.service._approvals.session_allows("user-one", "agent-one", self.chat, "sig"))

    def test_a_scope_the_card_did_not_offer_is_a_400_and_the_card_stays_pending(self) -> None:
        holder, thread, pending = self.ask()
        refused = self.client.post(f"{self.base}/approvals/{pending['id']}", json={"decision": "approve", "scope": "agent"})
        self.assertEqual(refused.status_code, 400, refused.text)
        self.assertEqual(len(self.client.get(f"{self.base}/approvals").json()), 1)
        self.client.post(f"{self.base}/approvals/{pending['id']}", json={"decision": "approve"})  # scope defaults to once
        thread.join(5)
        self.assertEqual(self.outcomes[0].scope, "once")

    def test_deny_ignores_the_scope(self) -> None:
        holder, thread, pending = self.ask()
        answer = self.client.post(f"{self.base}/approvals/{pending['id']}", json={"decision": "deny", "scope": "session"})
        self.assertEqual(answer.json(), {"status": "denied", "scope": "deny"})
        thread.join(5)
        self.assertFalse(self.service._approvals.session_allows("user-one", "agent-one", self.chat, "sig"))


class AgentScopeFailureTests(_ScopedFixture):
    options = ("once", "session", "agent")

    def test_a_failing_rule_write_still_approves_once_and_reports_the_error(self) -> None:
        def broken(rules):
            raise RuntimeError("disk is full")

        self.persisted = type("Broken", (), {"append": staticmethod(broken)})()
        holder, thread, pending = self.ask()
        answer = self.client.post(f"{self.base}/approvals/{pending['id']}", json={"decision": "approve", "scope": "agent"})
        body = answer.json()
        self.assertEqual((answer.status_code, body["status"], body["scope"]), (200, "approved", "once"))
        self.assertIn("disk is full", body["error"])
        thread.join(5)
        self.assertEqual(self.outcomes[0].scope, "once")

    def test_a_successful_rule_write_reports_scope_agent(self) -> None:
        holder, thread, pending = self.ask()
        answer = self.client.post(f"{self.base}/approvals/{pending['id']}", json={"decision": "approve", "scope": "agent"})
        self.assertEqual(answer.json(), {"status": "approved", "scope": "agent"})
        thread.join(5)
        self.assertEqual(self.persisted, [["rule"]])


if __name__ == "__main__":
    unittest.main()
