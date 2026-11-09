from __future__ import annotations

import os
import stat
import unittest
from unittest.mock import patch

from admin_helpers import AdminFixture

from src.config import secrets
from src.config.loader import load_agent
from src.terminal.output import REDACTED, clean, redact

LONG = "Zq9-longsecretvalue-123"


class SecretsFileTests(AdminFixture):
    def secrets_path(self):
        return self.config_dir / "secrets.env"

    def test_file_is_a_fallback_and_the_environment_wins(self) -> None:
        self.secrets_path().write_text(f"# comment\nSOME_KEY={LONG}\nexport QUOTED_KEY='quoted value'\n\nBAD LINE\n", encoding="utf-8")
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("SOME_KEY", None)
            self.assertEqual(secrets.get_secret("SOME_KEY"), LONG)
            self.assertEqual(secrets.get_secret("QUOTED_KEY"), "quoted value")
            self.assertEqual(secrets.get_secret("MISSING_KEY", "dflt"), "dflt")
            with patch.dict(os.environ, {"SOME_KEY": "from-env"}):
                self.assertEqual(secrets.get_secret("SOME_KEY"), "from-env")
            with patch.dict(os.environ, {"SOME_KEY": "  "}):  # blank does not shadow the file
                self.assertEqual(secrets.get_secret("SOME_KEY"), LONG)

    def test_changes_are_picked_up_without_a_restart(self) -> None:
        with patch.dict(os.environ):
            os.environ.pop("SOME_KEY", None)
            self.assertEqual(secrets.get_secret("SOME_KEY"), "")
            self.secrets_path().write_text("SOME_KEY=first-value\n", encoding="utf-8")
            self.assertEqual(secrets.get_secret("SOME_KEY"), "first-value")
            self.secrets_path().write_text("SOME_KEY=second-value-longer\n", encoding="utf-8")
            self.assertEqual(secrets.get_secret("SOME_KEY"), "second-value-longer")
            self.secrets_path().unlink()
            self.assertEqual(secrets.get_secret("SOME_KEY"), "")

    def test_provider_key_resolves_from_the_file(self) -> None:
        self.secrets_path().write_text(f"USER_A_KEY_NAME={LONG}\n", encoding="utf-8")
        with patch.dict(os.environ):
            os.environ.pop("USER_A_KEY_NAME", None)
            self.assertEqual(load_agent("user-a", "bot").api_key, LONG)
            os.environ["USER_A_KEY_NAME"] = "env-wins"
            self.assertEqual(load_agent("user-a", "bot").api_key, "env-wins")

    def test_set_secret_with_stdin_saves_without_printing_the_value(self) -> None:
        code, out, err = self.run_cli(["set-secret", "MY_NEW_KEY", "--stdin"], stdin=f"{LONG}\n")
        self.assertEqual((code, out.strip(), err), (0, "saved MY_NEW_KEY", ""))
        self.assertEqual(self.secrets_path().read_text(encoding="utf-8"), f"MY_NEW_KEY={LONG}\n")
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.secrets_path().stat().st_mode), 0o600)
        code, out, err = self.run_cli(["set-secret", "OTHER_KEY", "--stdin"], stdin="second-value-12345")
        self.assertEqual(code, 0)
        code, out, _ = self.run_cli(["set-secret", "MY_NEW_KEY", "--stdin"], stdin="rotated-value-67890\n")  # rotate in place
        self.assertEqual(self.secrets_path().read_text(encoding="utf-8"), "MY_NEW_KEY=rotated-value-67890\nOTHER_KEY=second-value-12345\n")
        self.assertNotIn("rotated-value", out)

    def test_set_secret_hidden_prompt_asks_twice_and_never_echoes(self) -> None:
        code, out, err = self.run_cli(["set-secret", "PROMPT_KEY"], passwords=[LONG, LONG])
        self.assertEqual((code, out.strip()), (0, "saved PROMPT_KEY"))
        self.assertNotIn(LONG, out + err)
        code, out, err = self.run_cli(["set-secret", "PROMPT_KEY"], passwords=[LONG, "different"])
        self.assertEqual(code, 1)
        self.assertNotIn(LONG, out + err)
        self.assertNotIn("different", out + err)

    def test_invalid_names_and_values_are_refused_and_nothing_is_written(self) -> None:
        for name in ("lower", "A", "1BAD_NAME", "HAS-DASH", "X" * 70, "SPACE NAME"):
            with self.subTest(name=name):
                code, out, err = self.run_cli(["set-secret", name, "--stdin"], stdin=LONG)
                self.assertEqual(code, 1)
                self.assertNotIn(LONG, out + err)
        code, _, err = self.run_cli(["set-secret", "EMPTY_KEY", "--stdin"], stdin="\n")
        self.assertEqual(code, 1)
        code, _, err = self.run_cli(["set-secret", "MULTI_KEY", "--stdin"], stdin="a\nb\n")
        self.assertEqual(code, 1)
        self.assertFalse(self.secrets_path().exists())

    def test_secret_list_shows_names_and_status_but_never_values(self) -> None:
        self.secrets_path().write_text(f"USER_A_KEY_NAME={LONG}\nSTRAY_KEY=stray-value-999\n", encoding="utf-8")
        with patch.dict(os.environ, {"TAVILY_API_KEY": "tavily-env-value-1"}):
            os.environ.pop("USER_A_KEY_NAME", None)
            code, out, err = self.run_cli(["secret", "list"])
        self.assertEqual(code, 0)
        self.assertRegex(out, r"USER_A_KEY_NAME\s+set \(secrets\.env\)")
        self.assertRegex(out, r"TAVILY_API_KEY\s+set \(environment\)")
        self.assertIn("STRAY_KEY", out)
        for value in (LONG, "stray-value-999", "tavily-env-value-1"):
            self.assertNotIn(value, out + err)


class RedactionTests(AdminFixture):
    def test_values_from_the_secrets_file_are_masked_in_terminal_output(self) -> None:
        (self.config_dir / "secrets.env").write_text(f"WEIRD_KEY={LONG}\nSHORT_KEY=short\n", encoding="utf-8")
        text = f"cat says WEIRD_KEY={LONG} and also {LONG}!\nshort stays: short"
        masked = redact(text)
        self.assertNotIn(LONG, masked)
        self.assertEqual(masked.count(REDACTED), 2)
        self.assertIn("short stays: short", masked)  # values under 8 characters are too ambiguous to mask
        self.assertNotIn(LONG, clean(text, 10_000)[0])

    def test_no_file_no_change(self) -> None:
        self.assertEqual(redact("plain text"), "plain text")


if __name__ == "__main__":
    unittest.main()
