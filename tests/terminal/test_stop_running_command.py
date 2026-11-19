"""Pressing Stop while a foreground command runs kills it promptly and ends the turn."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path

from src.config.models import LocalBackendConfig
from src.providers.cancellation import CancellationToken, GenerationCancelled
from src.terminal.backends import LocalBackend, Result
from src.terminal.context import RunContext, run_context
from tests.support.terminal_helpers import FakeBackend, make_session

POSIX = os.name != "nt"


def cancel_soon(token: CancellationToken, delay: float = 0.5) -> threading.Timer:
    timer = threading.Timer(delay, token.cancel, args=("stopped",))
    timer.start()
    return timer


@unittest.skipUnless(POSIX, "POSIX shell")
class RunProcessCancellationTests(unittest.TestCase):
    def test_cancelling_the_token_kills_the_command_within_a_second(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = LocalBackend("local", LocalBackendConfig(type="local", workdir=tmp))
            token = CancellationToken()
            pidfile = Path(tmp) / "pid"
            command = f"echo started; echo $$ > {pidfile}; sleep 30"
            timer = cancel_soon(token)
            started = time.monotonic()
            try:
                result = backend.run(command, "~/workspace", 60, cancellation=token)
            finally:
                timer.cancel()
            self.assertLess(time.monotonic() - started, 3)
            self.assertTrue(result.cancelled)
            self.assertIsNone(result.exit_code)
            self.assertFalse(result.timed_out)
            self.assertIn("started", result.stdout)
            time.sleep(0.2)
            with self.assertRaises(ProcessLookupError):
                os.kill(int(pidfile.read_text().strip()), 0)

    def test_the_current_run_token_is_the_default(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = LocalBackend("local", LocalBackendConfig(type="local", workdir=tmp))
            token = CancellationToken()
            ctx = RunContext("u", "a", "c", None, lambda *a: None, cancellation=token)
            timer = cancel_soon(token, 0.3)
            try:
                with run_context(ctx):
                    result = backend.run("sleep 30", "~/workspace", 60)
            finally:
                timer.cancel()
            self.assertTrue(result.cancelled)

    def test_a_normal_command_is_not_affected_and_gets_stdin(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = LocalBackend("local", LocalBackendConfig(type="local", workdir=tmp))
            result = backend.run("cat; sleep 0.5; echo done", "~/workspace", 30, stdin="hi\n", cancellation=CancellationToken())
        self.assertEqual((result.exit_code, result.cancelled), (0, False))
        self.assertEqual(result.stdout, "hi\ndone\n")

    def test_the_timeout_still_applies(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            backend = LocalBackend("local", LocalBackendConfig(type="local", workdir=tmp))
            result = backend.run("sleep 30", "~/workspace", 0.5, cancellation=CancellationToken())
        self.assertTrue(result.timed_out)
        self.assertFalse(result.cancelled)


class SessionCancellationTests(unittest.TestCase):
    def test_the_turn_is_cancelled_and_audited(self) -> None:
        token = CancellationToken()

        def handler(command, cwd, timeout, stdin):
            token.cancel("stopped")
            return Result(None, "partial", cancelled=True)

        with tempfile.TemporaryDirectory() as tmp:
            audit = Path(tmp) / "audit.jsonl"
            session = make_session(FakeBackend(handler), "off", audit_file=audit)
            ctx = RunContext("u", "a", "c", None, lambda *a: None, cancellation=token, interactive=True)
            with run_context(ctx), self.assertRaises(GenerationCancelled):
                session.run("terminal", "sleep 100", "sleep 100")
            record = json.loads(audit.read_text(encoding="utf-8").splitlines()[-1])
        self.assertTrue(record["cancelled"])
        self.assertIsNone(record["exit_code"])


if __name__ == "__main__":
    unittest.main()
