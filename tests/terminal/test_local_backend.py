"""The `type: local` backend as the guide agent uses it inside the container."""

from __future__ import annotations

import os
import re
import unittest
from pathlib import Path
from unittest.mock import patch

from src.config.models import LocalBackendConfig
from src.terminal.backends import LocalBackend, scrubbed_env

ROOT = Path(__file__).resolve().parents[2]
# utilities the sandbox_* snippets in src/terminal/files.py call; python:3.12-slim (Debian) ships all of them
POSIX_UTILITIES = {"head", "tr", "wc", "awk", "mktemp", "cat", "dirname", "mkdir", "chmod", "mv", "rm", "find", "grep"}


class LocalBackendTests(unittest.TestCase):
    def test_environment_hides_recall_settings_and_secrets_but_keeps_path(self) -> None:
        env = scrubbed_env({
            "PATH": "/app/.venv/bin:/usr/bin", "HOME": "/app", "RECALL_TERMINAL_ALLOW_LOCAL": "true",
            "RECALL_SESSION_SECRET": "s", "OPENROUTER_API_KEY": "k", "TELEGRAM_BOT_TOKEN": "t", "MY_SERVICE_PASSWORD": "p",
        })
        self.assertEqual(env, {"PATH": "/app/.venv/bin:/usr/bin", "HOME": "/app"})

    @unittest.skipIf(os.name == "nt", "POSIX shell lookup")
    def test_shell_falls_back_to_sh_when_bash_is_missing(self) -> None:
        with patch("src.terminal.backends.shutil.which", return_value=None):
            self.assertEqual(LocalBackend.shell(), ["/bin/sh", "-c"])
        with patch("src.terminal.backends.shutil.which", return_value="/bin/bash"):
            self.assertEqual(LocalBackend.shell(), ["/bin/bash", "-c"])

    def test_runs_in_its_workdir_with_the_scrubbed_environment(self) -> None:
        if LocalBackend.shell()[0].lower().endswith(("cmd.exe", "cmd")):
            self.skipTest("needs a POSIX shell")
        backend = LocalBackend("local", LocalBackendConfig(type="local", workdir=str(ROOT)))
        with patch.dict(os.environ, {"RECALL_TERMINAL_ALLOW_LOCAL": "true", "SOME_API_KEY": "hidden-value"}):
            result = backend.run('printf "%s|%s|%s" "${SOME_API_KEY-unset}" "${RECALL_TERMINAL_ALLOW_LOCAL-unset}" "$(basename "$PWD")"',
                                 "~/workspace", 30)
        self.assertEqual(result.exit_code, 0, result.stderr)
        self.assertEqual(result.stdout, f"unset|unset|{ROOT.name}")

    def test_dockerfile_installs_the_extras_and_documents_the_shell(self) -> None:
        text = (ROOT / "Dockerfile").read_text(encoding="utf-8")
        self.assertRegex(text, r"\n\s+file diffutils \\")
        self.assertTrue(text.index("file diffutils") > text.index("FROM python:3.12-slim AS runtime"))

    def test_every_utility_the_file_tools_call_is_known(self) -> None:
        source = (ROOT / "src" / "terminal" / "files.py").read_text(encoding="utf-8")
        for name in POSIX_UTILITIES:
            self.assertRegex(source, rf"\b{name}\b", name)
        self.assertNotRegex(source, r"\bfile -|\bdiff -")  # not used: difflib in Python, no `file` binary


if __name__ == "__main__":
    unittest.main()
