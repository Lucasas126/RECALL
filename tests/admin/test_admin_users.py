import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from src.admin import users
from src.config.loader import load_user
from src.web.auth import hash_password, verify_password

GOOD = "correct-horse-battery"


class AdminUsersTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for target in ("src.admin.users.CONFIG_DIR", "src.config.loader.CONFIG_DIR"):
            patcher = patch(target, self.root)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_cli(self, argv, passwords=()):
        out, err = io.StringIO(), io.StringIO()
        with patch("getpass.getpass", side_effect=list(passwords)), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = users.main(argv)
        return code, out.getvalue(), err.getvalue()

    def data(self, user_id="user-a"):
        return yaml.safe_load((self.root / user_id / "user.yaml").read_text(encoding="utf-8"))

    def test_create_user_is_loadable_and_password_verifies(self) -> None:
        code, out, _ = self.run_cli(["create-user", "user-a", "--name", "User A", "--api-key-env", "USER_A_KEY"], [GOOD, GOOD])
        self.assertEqual(code, 0)
        user = load_user("user-a")
        self.assertEqual((user.name, user.agents, user.language, user.provider.api_key), ("User A", [], "en", "USER_A_KEY"))
        self.assertTrue(verify_password(GOOD, user.password_hash))
        self.assertNotIn(GOOD, out)
        self.assertIn("TELEGRAM_ALLOWED_USERS", out)
        self.assertNotIn("claude auth login", out)

    def test_claude_login_sets_folder_and_prints_login_command(self) -> None:
        code, out, _ = self.run_cli(["create-user", "user-a", "--claude-login", "--generate", "--api-key-env", "K"])
        self.assertEqual(code, 0)
        folder = Path(load_user("user-a").claude_subscription.claude_config_dir)
        self.assertEqual(folder, (self.root / "user-a" / "claude-login").resolve())
        self.assertTrue(folder.is_dir())
        self.assertIn("claude auth login", out)

    def test_generate_prints_once_and_verifies(self) -> None:
        code, out, _ = self.run_cli(["create-user", "user-a", "--generate", "--api-key-env", "K"])
        self.assertEqual(code, 0)
        words = [w for w in out.split() if len(w) == users._GENERATED_LENGTH and set(w) <= set(users._ALPHABET)]
        self.assertEqual(len(words), 1)
        self.assertGreaterEqual(len(words[0]), 20)
        self.assertEqual(out.count(words[0]), 1)
        self.assertTrue(verify_password(words[0], load_user("user-a").password_hash))
        self.assertNotIn(words[0], (self.root / "user-a" / "user.yaml").read_text(encoding="utf-8"))

    def test_weak_short_and_mismatched_passwords_refused(self) -> None:
        for passwords in ([ "short", "short"], ["12345678", "12345678"], ["password", "password"], [GOOD, "other-one-here"]):
            code, _, err = self.run_cli(["create-user", "user-a", "--api-key-env", "K"], passwords)
            self.assertEqual(code, 1, passwords)
            self.assertIn("error", err)
            self.assertFalse((self.root / "user-a" / "user.yaml").exists())

    def test_existing_user_refused_and_untouched(self) -> None:
        self.run_cli(["create-user", "user-a", "--api-key-env", "K"], [GOOD, GOOD])
        before = (self.root / "user-a" / "user.yaml").read_text(encoding="utf-8")
        code, _, err = self.run_cli(["create-user", "user-a", "--api-key-env", "K"], ["another-pass-1", "another-pass-1"])
        self.assertEqual(code, 1)
        self.assertIn("set-password", err)
        self.assertEqual((self.root / "user-a" / "user.yaml").read_text(encoding="utf-8"), before)

    def test_user_id_validation(self) -> None:
        for bad in ("../x", "a b", "a/b", "", "x" * 65, "ñu"):
            code, _, err = self.run_cli(["create-user", bad, "--generate", "--api-key-env", "K"])
            self.assertEqual(code, 1, bad)
            self.assertIn("Invalid user id", err)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_api_key_env_must_be_a_name_not_a_key(self) -> None:
        code, _, err = self.run_cli(["create-user", "user-a", "--generate", "--api-key-env", "sk-or-v1-abc.def"])
        self.assertEqual(code, 1)
        self.assertIn("NAME", err)

    def test_set_password_keeps_other_keys(self) -> None:
        self.run_cli(["create-user", "user-a", "--name", "User A", "--claude-login", "--api-key-env", "K"], [GOOD, GOOD])
        path = self.root / "user-a" / "user.yaml"
        data = self.data()
        data["agents"] = ["aria"]
        data["require_zdr"] = True
        path.write_text(yaml.safe_dump(data), encoding="utf-8")
        code, out, _ = self.run_cli(["set-password", "user-a"], ["new-secret-pass", "new-secret-pass"])
        self.assertEqual(code, 0)
        self.assertIn("comments", out)
        after = self.data()
        self.assertTrue(verify_password("new-secret-pass", after.pop("password_hash")))
        data.pop("password_hash")
        self.assertEqual(after, data)

    def test_set_password_unknown_user_weak_and_generate(self) -> None:
        self.assertEqual(self.run_cli(["set-password", "ghost", "--generate"])[0], 1)
        self.run_cli(["create-user", "user-a", "--api-key-env", "K"], [GOOD, GOOD])
        old = self.data()["password_hash"]
        self.assertEqual(self.run_cli(["set-password", "user-a"], ["1234", "1234"])[0], 1)
        self.assertEqual(self.data()["password_hash"], old)
        code, out, _ = self.run_cli(["set-password", "user-a", "--generate"])
        generated = [w for w in out.split() if len(w) == users._GENERATED_LENGTH and set(w) <= set(users._ALPHABET)]
        self.assertEqual((code, len(generated)), (0, 1))
        self.assertTrue(verify_password(generated[0], self.data()["password_hash"]))

    def test_no_password_argument_exists(self) -> None:
        parser = users._build_parser()
        for argv in (["create-user", "user-a", "--password", "x"], ["set-password", "user-a", "--password", "x"]):
            with contextlib.redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                parser.parse_args(argv)
        self.assertEqual(hash_password(GOOD)[:9], "$argon2id")


if __name__ == "__main__":
    unittest.main()
