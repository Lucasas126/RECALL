from __future__ import annotations

import threading
import time
import unittest

from terminal_helpers import FakeBackend, Responder, StubBroker, make_session
from src.terminal.approvals import ApprovalBroker, ApprovalForbidden, ApprovalNotFound
from src.terminal.context import run_context
from src.utils.i18n import InputError

RM = "rm -rf " + "build"          # dangerous
HARDLINE = "rm -rf " + "/"        # hardline


class Asker:
    """Runs broker.request in a thread and waits until the approval is pending."""

    def __init__(self, broker: ApprovalBroker, *, user="u", timeout=5.0, run=None):
        self.broker = broker
        self.run = run or Responder(broker, answer=None)
        self.outcome = None
        self.thread = threading.Thread(target=self._go, args=(user, timeout), daemon=True)
        self.thread.start()
        deadline = time.monotonic() + 3
        while not broker.pending("u", "a", "chat1"):
            assert time.monotonic() < deadline, "approval never became pending"
            time.sleep(0.01)
        self.id = broker.pending("u", "a", "chat1")[0]["id"]

    def _go(self, user, timeout):
        self.outcome = self.broker.request(user, "a", "chat1", self.run, "terminal", "ls", "~/w", "why", "manual",
                                           backend="pc", timeout_seconds=timeout)

    def join(self):
        self.thread.join(3)
        assert not self.thread.is_alive()
        return self.outcome


class BrokerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.broker = ApprovalBroker(5)
        self.addCleanup(self.broker.close)

    def test_approve_resumes_the_tool_and_emits_both_events(self) -> None:
        asker = Asker(self.broker)
        payload = asker.run.events[0]
        self.assertEqual(payload[0], "approval")
        for key in ("id", "tool", "command", "cwd", "reason", "risk", "timeout_seconds", "expires_at", "chat_id", "agent_id", "backend"):
            self.assertIn(key, payload[1])
        self.assertEqual(payload[1]["backend"], "pc")
        self.assertEqual(self.broker.resolve(asker.id, "u", "approve"), "approved")
        outcome = asker.join()
        self.assertTrue(outcome.approved)
        self.assertEqual(self.broker.recent_decisions("u")[-1]["backend"], "pc")
        self.assertEqual(asker.run.kinds(), ["approval", "approval_resolved"])
        self.assertEqual(asker.run.events[1][1], {"id": asker.id, "decision": "approved", "reason": "approved"})
        self.assertEqual(self.broker.pending("u", "a", "chat1"), [])

    def test_deny(self) -> None:
        asker = Asker(self.broker)
        self.assertEqual(self.broker.resolve(asker.id, "u", "deny"), "denied")
        outcome = asker.join()
        self.assertFalse(outcome.approved)
        self.assertEqual(outcome.reason, "denied by user")

    def test_timeout_denies(self) -> None:
        asker = Asker(self.broker, timeout=0.05)
        outcome = asker.join()
        self.assertFalse(outcome.approved)
        self.assertEqual(outcome.reason, "timed out")
        self.assertEqual(asker.run.events[-1][1]["reason"], "timed out")
        with self.assertRaises(ApprovalNotFound):  # an expired approval cannot be revived
            self.broker.resolve(asker.id, "u", "approve")

    def test_cancelling_the_run_denies(self) -> None:
        asker = Asker(self.broker)
        asker.run.cancellation.cancel()
        self.assertEqual(asker.join().reason, "cancelled")

    def test_only_the_owner_may_resolve(self) -> None:
        asker = Asker(self.broker)
        with self.assertRaises(ApprovalForbidden):
            self.broker.resolve(asker.id, "intruder", "approve")
        self.assertEqual(len(self.broker.pending("u", "a", "chat1")), 1)  # still waiting
        self.assertEqual(self.broker.pending("intruder", "a", "chat1"), [])
        self.broker.resolve(asker.id, "u", "deny")
        asker.join()
        with self.assertRaises(ApprovalForbidden):  # also after it closed
            self.broker.resolve(asker.id, "intruder", "approve")

    def test_resolving_twice_is_idempotent_and_first_answer_wins(self) -> None:
        asker = Asker(self.broker)
        self.assertEqual(self.broker.resolve(asker.id, "u", "approve"), "approved")
        self.assertEqual(self.broker.resolve(asker.id, "u", "approve"), "approved")
        self.assertEqual(self.broker.resolve(asker.id, "u", "deny"), "approved")
        self.assertTrue(asker.join().approved)

    def test_unknown_id_and_bad_decision(self) -> None:
        with self.assertRaises(ApprovalNotFound):
            self.broker.resolve("nope", "u", "approve")
        asker = Asker(self.broker)
        with self.assertRaises(InputError):
            self.broker.resolve(asker.id, "u", "maybe")

    def test_shutdown_denies_waiting_tools_and_refuses_new_ones(self) -> None:
        asker = Asker(self.broker)
        self.broker.close()
        self.assertEqual(asker.join().reason, "service shutdown")
        late = self.broker.request("u", "a", "chat1", Responder(self.broker, None), "terminal", "ls", "~", "", "manual")
        self.assertFalse(late.approved)

    def test_decision_log_keeps_the_recent_answers(self) -> None:
        asker = Asker(self.broker)
        self.broker.resolve(asker.id, "u", "approve")
        asker.join()
        log = self.broker.recent_decisions("u")
        self.assertEqual([(e["id"], e["decision"], e["tool"]) for e in log], [(asker.id, "approved", "terminal")])
        self.assertEqual(self.broker.recent_decisions("other"), [])


class UnattendedTests(unittest.TestCase):
    """Dream sessions, scripts and runs without a client never ask: manual and dangerous are denied."""

    def run_cmd(self, command, approvals, *, how):
        backend = FakeBackend()
        session = make_session(backend, approvals, unattended=(how == "flag"))
        responder = Responder(ApprovalBroker(1), answer="approve")
        if how == "no-context":
            result, denial = session.run("terminal", command, command)
        else:
            with run_context(responder.context(interactive=(how != "non-interactive"))):
                result, denial = session.run("terminal", command, command)
        self.assertEqual(responder.events, [])  # nobody was ever asked
        return backend, result, denial

    def test_matrix(self) -> None:
        cases = [  # (mode, command, runs?)
            ("manual", "ls", False), ("manual", RM, False),
            ("smart", "ls", True), ("smart", RM, False),
            ("off", "ls", True), ("off", RM, False),  # `off` never widens what runs unattended
            ("manual", HARDLINE, False), ("smart", HARDLINE, False), ("off", HARDLINE, False),
        ]
        for how in ("flag", "no-context", "non-interactive"):
            for mode, command, runs in cases:
                with self.subTest(how=how, mode=mode, command=command):
                    backend, result, denial = self.run_cmd(command, mode, how=how)
                    self.assertEqual(bool(backend.calls), runs)
                    self.assertEqual(denial is None, runs)


class SessionPolicyTests(unittest.TestCase):
    def run_in_mode(self, mode, command, *, approved=True):
        backend, broker = FakeBackend(), StubBroker(approved)
        responder = Responder(broker, None)  # type: ignore[arg-type]
        with run_context(responder.context()):
            result, denial = make_session(backend, mode).run("terminal", command, command)
        return backend, broker, result, denial

    def test_manual_asks_for_everything_reads_included(self) -> None:
        backend, broker, result, _ = self.run_in_mode("manual", "ls")
        self.assertEqual([r["risk"] for r in broker.requests], ["manual"])
        self.assertIsNotNone(result)
        self.assertEqual(len(backend.calls), 1)

    def test_the_request_names_the_backend(self) -> None:
        _, broker, _, _ = self.run_in_mode("manual", "ls")
        self.assertEqual(broker.requests[0]["backend"], "box")

    def test_smart_asks_only_for_dangerous_commands(self) -> None:
        _, broker, result, _ = self.run_in_mode("smart", "ls")
        self.assertEqual(broker.requests, [])
        self.assertIsNotNone(result)
        backend, broker, result, _ = self.run_in_mode("smart", RM)
        self.assertEqual([r["risk"] for r in broker.requests], ["dangerous"])
        self.assertTrue(broker.requests[0]["reason"])
        self.assertIsNotNone(result)

    def test_denied_approval_means_the_command_never_runs(self) -> None:
        backend, broker, result, denial = self.run_in_mode("smart", RM, approved=False)
        self.assertIsNone(result)
        self.assertIn("Denied", denial)
        self.assertIn("Do not retry", denial)
        self.assertEqual(backend.calls, [])

    def test_hardline_is_denied_in_every_mode_without_asking(self) -> None:
        for mode in ("manual", "smart", "off"):
            with self.subTest(mode=mode):
                backend, broker, result, denial = self.run_in_mode(mode, HARDLINE)
                self.assertIsNone(result)
                self.assertIn("hardline", denial)
                self.assertEqual((backend.calls, broker.requests), ([], []))

    def test_off_never_asks_even_for_dangerous(self) -> None:
        backend, broker, result, _ = self.run_in_mode("off", RM)
        self.assertEqual((broker.requests, len(backend.calls)), ([], 1))

    def test_timeout_is_clamped_and_nul_refused(self) -> None:
        session = make_session(FakeBackend(), "off", timeout_seconds=30)
        self.assertEqual(session.timeout_for(None), 30.0)
        self.assertEqual(session.timeout_for(100000), 600.0)
        self.assertEqual(session.timeout_for(0), 1.0)
        _, denial = session.run("terminal", "ls\x00", "ls\x00")
        self.assertIn("NUL", denial)


if __name__ == "__main__":
    unittest.main()
