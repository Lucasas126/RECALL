from __future__ import annotations

import contextlib
import io
import re
import tomllib
import unittest
from pathlib import Path
from unittest.mock import patch

import test_user_preferences as preferences_tests
from src.admin import users
from src.utils import version

ROOT = Path(__file__).resolve().parents[1]


class VersionAccessorTests(unittest.TestCase):
    def setUp(self) -> None:
        version.app_version.cache_clear()
        self.addCleanup(version.app_version.cache_clear)

    def test_reads_pyproject(self) -> None:
        expected = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
        self.assertEqual(version.app_version(), expected)

    def test_release_guard_version_is_plain_semver(self) -> None:
        self.assertRegex(version.app_version(), r"^\d+\.\d+\.\d+$")

    def test_falls_back_to_package_metadata_then_unknown(self) -> None:
        with patch.object(version, "_PYPROJECT", ROOT / "missing.toml"):
            with patch.object(version.metadata, "version", return_value="9.9.9"):
                self.assertEqual(version.app_version(), "9.9.9")
            version.app_version.cache_clear()
            with patch.object(version.metadata, "version", side_effect=version.metadata.PackageNotFoundError):
                self.assertEqual(version.app_version(), "unknown")

    def test_build_is_shortened_and_only_present_when_set(self) -> None:
        with patch.dict("os.environ", {"RECALL_BUILD": "0123456789abcdef"}):
            self.assertEqual(version.app_build(), "0123456")
            self.assertTrue(version.version_label().endswith(" (0123456)"))
        with patch.dict("os.environ", {"RECALL_BUILD": ""}):
            self.assertIsNone(version.app_build())
            self.assertEqual(version.version_label(), f"RECALL v{version.app_version()}")


class VersionSurfacesTests(unittest.TestCase):
    def test_cli_prints_version(self) -> None:
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = users.main(["version"])
        self.assertEqual(code, 0)
        self.assertEqual(out.getvalue().strip(), version.version_label())

    def test_telegram_help_starts_with_version(self) -> None:
        import asyncio
        from unittest.mock import AsyncMock, MagicMock

        from src.telegram import bot

        update = MagicMock()
        update.effective_message.reply_text = AsyncMock()
        with patch.object(bot, "_session", return_value=object()):
            asyncio.run(bot.help_command(update, MagicMock()))
        text = update.effective_message.reply_text.call_args.args[0]
        self.assertTrue(text.startswith(version.version_label() + "\n"))


class VersionWebTests(unittest.TestCase):
    setUp = preferences_tests.UserPreferencesWebTests.setUp

    def test_endpoint_is_public_and_has_only_version_and_build(self) -> None:
        with patch.dict("os.environ", {"RECALL_BUILD": "abcdef1234567"}):
            response = self.client.get("/api/version")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"version": version.app_version(), "build": "abcdef1"})
        with patch.dict("os.environ", {"RECALL_BUILD": ""}):
            self.assertIsNone(self.client.get("/api/version").json()["build"])

    def test_spanish_catalog_has_the_version_templates(self) -> None:
        from src.utils.i18n import messages

        for template in ("RECALL v{version}", "RECALL v{version} ({build})"):
            self.assertIn(template, messages("es"))


if __name__ == "__main__":
    unittest.main()
