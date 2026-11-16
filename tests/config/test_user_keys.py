"""A user's own OpenRouter key from the web UI: per-user variable, validation, isolation, removal."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
import yaml

from src.config import secrets, settings, user_keys
from src.utils.i18n import InputError

KEY = "sk-or-v1-" + "a" * 40
OTHER = "sk-or-v1-" + "b" * 40


def reply(status: int):
    return httpx.Response(status, request=httpx.Request("GET", user_keys.CHECK_URL))


class UserKeyTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        for user in ("user-a", "user-b"):
            (self.root / user).mkdir()
            (self.root / user / "user.yaml").write_text(yaml.safe_dump({
                "id": user, "password_hash": "HASH-" + user,
                "provider": {"name": "openrouter", "api_key": "OPENROUTER_API_KEY"},
            }), encoding="utf-8")
        (self.root / "secrets.env").write_text("# keep\nOPENROUTER_API_KEY=shared\nOTHER_SECRET=x\n", encoding="utf-8")
        patcher = patch.object(settings, "CONFIG_DIR", self.root)
        patcher.start()
        self.addCleanup(patcher.stop)
        env = patch.dict(os.environ)
        env.start()
        self.addCleanup(env.stop)
        for name in ("OPENROUTER_API_KEY", "OPENROUTER_API_KEY_USER_A", "OPENROUTER_API_KEY_USER_B"):
            os.environ.pop(name, None)

    def yaml_of(self, user):
        return yaml.safe_load((self.root / user / "user.yaml").read_text(encoding="utf-8"))

    def test_variable_name_is_per_user(self):
        self.assertEqual("OPENROUTER_API_KEY_USER_A", user_keys.variable_for("user-a"))
        self.assertNotEqual(user_keys.variable_for("user-a"), user_keys.variable_for("user-b"))

    def test_save_stores_the_key_under_the_users_variable_and_points_only_that_user_at_it(self):
        with patch("httpx.get", return_value=reply(200)):
            variable = user_keys.save_user_key("user-a", "  " + KEY + " ")
        self.assertEqual("OPENROUTER_API_KEY_USER_A", variable)
        values = secrets.parse((self.root / "secrets.env").read_text(encoding="utf-8"))
        self.assertEqual(KEY, values[variable])
        self.assertEqual("shared", values["OPENROUTER_API_KEY"])  # the shared key is untouched
        self.assertEqual("x", values["OTHER_SECRET"])
        self.assertEqual(variable, self.yaml_of("user-a")["provider"]["api_key"])
        self.assertEqual("HASH-user-a", self.yaml_of("user-a")["password_hash"])  # nothing else changed
        self.assertEqual("OPENROUTER_API_KEY", self.yaml_of("user-b")["provider"]["api_key"])

    def test_two_users_keep_separate_keys(self):
        with patch("httpx.get", return_value=reply(200)):
            user_keys.save_user_key("user-a", KEY)
            user_keys.save_user_key("user-b", OTHER)
        values = secrets.parse((self.root / "secrets.env").read_text(encoding="utf-8"))
        self.assertEqual(KEY, values["OPENROUTER_API_KEY_USER_A"])
        self.assertEqual(OTHER, values["OPENROUTER_API_KEY_USER_B"])

    def test_a_refused_key_is_not_saved(self):
        with patch("httpx.get", return_value=reply(401)):
            with self.assertRaises(InputError) as caught:
                user_keys.save_user_key("user-a", KEY)
        self.assertEqual("openrouter_key_refused", caught.exception.code)
        self.assertNotIn("USER_A", (self.root / "secrets.env").read_text(encoding="utf-8"))
        self.assertEqual("OPENROUTER_API_KEY", self.yaml_of("user-a")["provider"]["api_key"])

    def test_an_unreachable_check_does_not_save_either(self):
        with patch("httpx.get", side_effect=httpx.ConnectError("down")):
            with self.assertRaises(InputError) as caught:
                user_keys.save_user_key("user-a", KEY)
        self.assertEqual("openrouter_key_unchecked", caught.exception.code)

    def test_bad_shapes_never_reach_the_network(self):
        with patch("httpx.get") as get:
            for bad in ("", "short", "has space in the key value", "line\nbreak" + "x" * 20, "é" * 30):
                with self.assertRaises(InputError) as caught:
                    user_keys.save_user_key("user-a", bad)
                self.assertEqual("openrouter_key_bad_format", caught.exception.code)
        get.assert_not_called()

    def test_errors_never_contain_the_key(self):
        with patch("httpx.get", return_value=reply(401)):
            with self.assertRaises(InputError) as caught:
                user_keys.save_user_key("user-a", KEY)
        self.assertNotIn(KEY, str(caught.exception))

    def test_state_reports_own_versus_shared_without_the_value(self):
        os.environ["OPENROUTER_API_KEY"] = "shared"
        self.assertEqual({"configured": True, "own": False}, user_keys.key_state("user-a", "OPENROUTER_API_KEY"))
        with patch("httpx.get", return_value=reply(200)):
            user_keys.save_user_key("user-a", KEY)
        self.assertEqual({"configured": True, "own": True}, user_keys.key_state("user-a", "OPENROUTER_API_KEY_USER_A"))

    def test_remove_deletes_only_that_users_line(self):
        with patch("httpx.get", return_value=reply(200)):
            user_keys.save_user_key("user-a", KEY)
            user_keys.save_user_key("user-b", OTHER)
        user_keys.remove_user_key("user-a")
        text = (self.root / "secrets.env").read_text(encoding="utf-8")
        self.assertNotIn("USER_A", text)
        self.assertIn("OPENROUTER_API_KEY_USER_B=" + OTHER, text)
        self.assertIn("# keep", text)
        self.assertEqual(0o600, (self.root / "secrets.env").stat().st_mode & 0o777)

    def test_removing_a_key_that_does_not_exist_is_harmless(self):
        user_keys.remove_user_key("user-a")
        self.assertIn("OPENROUTER_API_KEY=shared", (self.root / "secrets.env").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
