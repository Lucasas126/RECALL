"""Web sign-in to a Claude subscription: the URL, the pasted code, refusals, one login per user, cleanup."""
import json
import os
import stat
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path
from unittest.mock import patch

from src.config.models import ClaudeSubscriptionConfig
from src.providers.claude_subscription import signin
from src.utils.i18n import InputError

GOOD_CODE = "goodcode1234#state5678"

FAKE = textwrap.dedent('''\
    #!{python}
    import json, os, sys
    args = sys.argv[1:]
    folder = os.environ["CLAUDE_CONFIG_DIR"]
    marker = os.path.join(folder, "logged-in")
    if args[:2] == ["auth", "status"]:
        print(json.dumps({{"loggedIn": os.path.exists(marker), "authMethod": "claude.ai", "subscriptionType": "max"}}))
    elif args[:2] == ["auth", "login"]:
        print("Opening browser to sign in...", flush=True)
        print("If the browser didn't open, visit: https://claude.example/cai/oauth/authorize?code=true&state=abc", flush=True)
        print("Paste code here if prompted > ", end="", flush=True)
        code = sys.stdin.readline().strip()
        if code == "{good}":
            open(marker, "w").close()
        else:
            print("Login failed: That code wasn't accepted.", flush=True)
            sys.exit(1)
''')


class SigninTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        script = root / "claude"
        script.write_text(FAKE.format(python=sys.executable, good=GOOD_CODE), encoding="utf-8")
        script.chmod(script.stat().st_mode | stat.S_IEXEC)
        self.folder = root / "login"
        self.claude = ClaudeSubscriptionConfig(claude_config_dir=self.folder.as_posix(), command="claude")
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        os.environ["PATH"] = root.as_posix() + os.pathsep + os.environ["PATH"]
        for key in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL"):
            os.environ.pop(key, None)
        self.addCleanup(signin.cancel, "user-a")
        self.addCleanup(signin.cancel, "user-b")

    def test_start_returns_the_login_url_and_creates_the_folder(self):
        result = signin.start("user-a", self.claude)
        self.assertEqual("https://claude.example/cai/oauth/authorize?code=true&state=abc", result["url"])
        self.assertTrue(self.folder.is_dir())

    def test_a_good_code_logs_in(self):
        signin.start("user-a", self.claude)
        status = signin.submit("user-a", GOOD_CODE, self.claude)
        self.assertTrue(status["logged_in"])
        self.assertEqual("max", status["subscription"])

    def test_a_wrong_code_is_refused_with_a_coded_error(self):
        signin.start("user-a", self.claude)
        with self.assertRaises(InputError) as caught:
            signin.submit("user-a", "wrongcode1234", self.claude)
        self.assertEqual("claude_login_refused", caught.exception.code)

    def test_a_code_without_a_started_login_is_refused(self):
        with self.assertRaises(InputError) as caught:
            signin.submit("user-a", GOOD_CODE, self.claude)
        self.assertEqual("claude_login_not_started", caught.exception.code)

    def test_a_code_with_odd_characters_never_reaches_the_cli(self):
        signin.start("user-a", self.claude)
        for bad in ("", "short", "has space in it 123", "line\nbreak12345", "x" * 600):
            with self.assertRaises(InputError) as caught:
                signin.submit("user-a", bad, self.claude)
            self.assertEqual("claude_login_bad_code", caught.exception.code)

    def test_a_code_is_single_use(self):
        signin.start("user-a", self.claude)
        signin.submit("user-a", GOOD_CODE, self.claude)
        with self.assertRaises(InputError) as caught:
            signin.submit("user-a", GOOD_CODE, self.claude)
        self.assertEqual("claude_login_not_started", caught.exception.code)

    def test_starting_again_replaces_the_waiting_login(self):
        signin.start("user-a", self.claude)
        first = signin._sessions["user-a"].process
        signin.start("user-a", self.claude)
        self.assertIsNotNone(first.poll() if first.poll() is not None else (first.wait(timeout=5)))
        self.assertIsNot(first, signin._sessions["user-a"].process)

    def test_users_do_not_share_a_waiting_login(self):
        signin.start("user-a", self.claude)
        with self.assertRaises(InputError) as caught:
            signin.submit("user-b", GOOD_CODE, self.claude)
        self.assertEqual("claude_login_not_started", caught.exception.code)

    def test_cancel_kills_the_process(self):
        signin.start("user-a", self.claude)
        process = signin._sessions["user-a"].process
        signin.cancel("user-a")
        process.wait(timeout=5)
        self.assertNotIn("user-a", signin._sessions)

    def test_missing_cli_is_a_coded_error(self):
        missing = self.claude.model_copy(update={"command": "claude-not-installed"})
        with self.assertRaises(InputError) as caught:
            signin.start("user-a", missing)
        self.assertEqual("claude_not_installed", caught.exception.code)

    def test_no_url_means_a_coded_error_and_no_leftover_process(self):
        with patch.object(signin, "URL_WAIT", 0.5), patch.object(signin, "_URL", signin.re.compile(r"never-matches")):
            with self.assertRaises(InputError) as caught:
                signin.start("user-a", self.claude)
        self.assertEqual("claude_login_no_url", caught.exception.code)
        self.assertNotIn("user-a", signin._sessions)


if __name__ == "__main__":
    unittest.main()
