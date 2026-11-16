"""Runs the Node tests (`tests/web/js/*.cjs`) as part of the Python suite.

They drive the web UI's `app.js` in a fake DOM. Nothing else ran them, so one of them
(`test_model_sorting.cjs`) stayed red for weeks while the Python suite was green.
"""
from __future__ import annotations

import shutil
import subprocess
import unittest
from pathlib import Path

TESTS = Path(__file__).resolve().parent


@unittest.skipUnless(shutil.which("node"), "node is not installed")
class NodeSuiteTests(unittest.TestCase):
    def test_every_node_test_file_passes(self) -> None:
        files = sorted(TESTS.glob("*.cjs"))
        self.assertTrue(files, "no Node tests found")
        failures = []
        for file in files:
            # a per-test timeout, so a promise that never resolves fails instead of hanging the suite
            result = subprocess.run(
                ["node", "--test", "--test-timeout=20000", str(file)],
                capture_output=True, text=True, timeout=120,
            )
            if result.returncode != 0:
                failures.append(f"{file.name}:\n{(result.stdout + result.stderr)[-1500:]}")
        self.assertFalse(failures, "\n\n".join(failures))


if __name__ == "__main__":
    unittest.main()
