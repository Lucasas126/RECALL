"""Background processes, sub-step (a): the registry and the local backend."""

import sys
import tempfile
import time
import unittest
from pathlib import Path

from src.terminal.backends import scrubbed_env
from src.terminal.processes import ProcessError, ProcessRegistry


class RegistryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.dir.cleanup)
        self.registry = ProcessRegistry(Path(self.dir.name), log_cap=1000, max_per_chat=2, max_per_user=3)
        self.addCleanup(lambda: [self.registry.stop_chat("user-a", c) for c in ("c1", "c2")])

    def start(self, script: str, chat: str = "c1", **kwargs) -> dict:
        return self.registry.start_local(
            user_id="user-a", agent_id="main", chat_id=chat, backend="box", command=script,
            argv=[sys.executable, "-c", script], cwd=self.dir.name, env=scrubbed_env(), **kwargs,
        )

    def test_a_finished_process_records_exit_code_and_output(self) -> None:
        record = self.start("print('hello'); print('bye')")
        self.assertEqual(record["state"], "running")
        done = self.registry.wait("user-a", record["id"], 20)
        self.assertEqual((done["state"], done["exit_code"]), ("exited", 0))
        self.assertEqual(self.registry.read("user-a", record["id"])["output"], "hello\nbye")

    def test_a_failing_process_keeps_its_exit_code_and_stderr(self) -> None:
        record = self.start("import sys; print('oops', file=sys.stderr); sys.exit(3)")
        done = self.registry.wait("user-a", record["id"], 20)
        self.assertEqual(done["exit_code"], 3)
        self.assertIn("oops", self.registry.read("user-a", record["id"])["output"])

    def test_tail_limits_the_lines_returned(self) -> None:
        record = self.start("[print(i) for i in range(20)]")
        self.registry.wait("user-a", record["id"], 20)
        out = self.registry.read("user-a", record["id"], tail=3)
        self.assertEqual((out["output"], out["output_truncated"], out["output_lines"]), ("17\n18\n19", True, 20))

    def test_the_log_keeps_only_the_last_bytes(self) -> None:
        record = self.start("print('x' * 100) if False else [print('line %04d' % i) for i in range(1000)]")
        self.registry.wait("user-a", record["id"], 20)
        size = self.registry.log_path("user-a", record["id"]).stat().st_size
        self.assertLessEqual(size, 2000)
        self.assertIn("line 0999", self.registry.read("user-a", record["id"])["output"])

    def test_wait_returns_while_still_running_after_the_timeout(self) -> None:
        record = self.start("import time; time.sleep(30)")
        started = time.monotonic()
        waited = self.registry.wait("user-a", record["id"], 0.5)
        self.assertEqual(waited["state"], "running")
        self.assertLess(time.monotonic() - started, 5)

    def test_stop_kills_the_whole_group(self) -> None:
        record = self.start("import subprocess, time; subprocess.Popen(['sleep', '60']); time.sleep(60)")
        stopped = self.registry.stop("user-a", record["id"])
        self.assertEqual((stopped["state"], stopped["exit_code"]), ("stopped", None))
        with self.assertRaises(ProcessError):
            self.registry.stop("user-a", record["id"])

    def test_the_lifetime_limit_stops_it(self) -> None:
        record = self.start("import time; time.sleep(60)", max_lifetime=1)
        done = self.registry.wait("user-a", record["id"], 20)
        self.assertEqual(done["state"], "timed_out")

    def test_limits_per_chat_and_per_user(self) -> None:
        self.start("import time; time.sleep(30)")
        self.start("import time; time.sleep(30)")
        with self.assertRaises(ProcessError) as caught:
            self.start("print(1)")
        self.assertIn("this chat", str(caught.exception))
        self.start("import time; time.sleep(30)", chat="c2")
        with self.assertRaises(ProcessError) as caught:
            self.start("print(1)", chat="c2")
        self.assertIn("this user", str(caught.exception))

    def test_unknown_and_malformed_ids_are_refused(self) -> None:
        for bad in ("p00000000", "../x", "nope"):
            with self.assertRaises(ProcessError):
                self.registry.read("user-a", bad)

    def test_a_missing_binary_is_a_clean_error(self) -> None:
        with self.assertRaises(ProcessError):
            self.registry.start_local(user_id="user-a", agent_id="m", chat_id="c1", backend="b", command="x",
                                      argv=["/nonexistent-binary"], cwd=self.dir.name, env={})

    def test_finish_hook_runs_and_recover_marks_orphans_lost(self) -> None:
        seen = []
        self.registry.subscribe_finish(seen.append)
        record = self.start("print(1)")
        self.registry.wait("user-a", record["id"], 20)
        deadline = time.monotonic() + 5
        while not seen and time.monotonic() < deadline:
            time.sleep(0.05)
        self.assertEqual([r["id"] for r in seen], [record["id"]])
        orphan = dict(record, id="p" + "0" * 7 + "1", state="running", ended=None)
        self.registry._save(orphan)
        fresh = ProcessRegistry(Path(self.dir.name))
        self.assertEqual(fresh.recover("user-a"), 1)
        self.assertEqual(fresh.get("user-a", orphan["id"])["state"], "lost")


if __name__ == "__main__":
    unittest.main()
