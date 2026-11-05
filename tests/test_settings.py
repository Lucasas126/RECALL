from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path

import yaml

_ROOT = Path(__file__).resolve().parents[1]


def _web_host(env_value: str | None) -> str:
    """Import the settings in a fresh interpreter and return the resolved web host."""
    env = {key: value for key, value in os.environ.items() if key != "RECALL_WEB_HOST"}
    if env_value is not None:
        env["RECALL_WEB_HOST"] = env_value
    result = subprocess.run(
        [sys.executable, "-c", "from src.config.settings import WEB_HOST; print(WEB_HOST)"],
        cwd=_ROOT, env=env, capture_output=True, text=True, check=True,
    )
    return result.stdout.strip()


class WebHostSettingTests(unittest.TestCase):
    def test_settings_yaml_host_is_the_default(self) -> None:
        configured = yaml.safe_load((_ROOT / "settings.yaml").read_text(encoding="utf-8"))["web"]["host"]
        self.assertEqual(_web_host(None), configured)

    def test_environment_overrides_host(self) -> None:
        self.assertEqual(_web_host("0.0.0.0"), "0.0.0.0")

    def test_empty_environment_value_keeps_default(self) -> None:
        self.assertEqual(_web_host(""), _web_host(None))


def _allow_local(env_value: str | None) -> subprocess.CompletedProcess:
    env = {key: value for key, value in os.environ.items() if key != "RECALL_TERMINAL_ALLOW_LOCAL"}
    if env_value is not None:
        env["RECALL_TERMINAL_ALLOW_LOCAL"] = env_value
    return subprocess.run(
        [sys.executable, "-c", "from src.config.settings import TERMINAL; print(TERMINAL.allow_local)"],
        cwd=_ROOT, env=env, capture_output=True, text=True,
    )


class AllowLocalOverrideTests(unittest.TestCase):
    def test_settings_yaml_is_the_default(self) -> None:
        configured = yaml.safe_load((_ROOT / "settings.yaml").read_text(encoding="utf-8"))["terminal"]["allow_local"]
        self.assertEqual(_allow_local(None).stdout.strip(), str(configured))
        self.assertEqual(_allow_local("").stdout.strip(), str(configured))

    def test_environment_overrides_in_both_directions(self) -> None:
        for value in ("true", "TRUE", "1", "yes", "on"):
            self.assertEqual(_allow_local(value).stdout.strip(), "True", value)
        for value in ("false", "0", "no", "off"):
            self.assertEqual(_allow_local(value).stdout.strip(), "False", value)

    def test_garbage_is_refused_with_the_variable_name(self) -> None:
        result = _allow_local("maybe")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("RECALL_TERMINAL_ALLOW_LOCAL", result.stderr)

    def test_parser_leaves_non_boolean_yaml_to_the_strict_model(self) -> None:
        from src.config.settings import parse_allow_local

        self.assertIs(parse_allow_local(None), False)
        self.assertIs(parse_allow_local(False, "true"), True)
        self.assertEqual(parse_allow_local("yes"), "yes")  # TerminalSettings (strict) rejects it


if __name__ == "__main__":
    unittest.main()
