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


if __name__ == "__main__":
    unittest.main()
