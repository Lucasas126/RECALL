from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import yaml
from pydantic import ValidationError

from src.agent.profile import AgentProfile, load_profile, system_prompt
from src.config import settings
from src.config.loader import load_agent
from src.config.models import AgentConfig, MemoryLimits
from src.core.sources.registry import SourceRegistry
from src.utils.i18n import InputError


def _limits(**values) -> object:
    return patch("src.config.settings.MEMORY_LIMITS", MemoryLimits(**values))


class MemoryLimitsModelTests(unittest.TestCase):
    def test_defaults(self) -> None:
        limits = MemoryLimits()
        self.assertEqual((limits.soul_chars, limits.memory_chars, limits.user_chars), (15000, 2500, 2500))

    def test_bounds_and_unknown_keys_are_rejected(self) -> None:
        for bad in ({"soul_chars": 999}, {"soul_chars": 100_001}, {"memory_chars": 99}, {"memory_chars": 50_001},
                    {"user_chars": 99}, {"user_chars": 50_001}, {"user_chars": "300"}, {"other": 1}):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                MemoryLimits(**bad)
        MemoryLimits(soul_chars=1000, memory_chars=100, user_chars=50_000)

    def test_repository_settings_use_the_defaults(self) -> None:
        raw = yaml.safe_load((Path(settings.__file__).resolve().parents[2] / "settings.yaml").read_text(encoding="utf-8"))
        self.assertEqual(MemoryLimits.model_validate(raw["memory_limits"]), MemoryLimits())
        self.assertEqual(settings.memory_limits(), settings.MEMORY_LIMITS)


class UnknownAgentKeyTests(unittest.TestCase):
    def test_unknown_keys_still_fail(self) -> None:
        with self.assertRaises(ValidationError):
            AgentConfig.model_validate({"id": "a", "name": "A", "bogus": 1})


class AppLimitsAreUsedTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.sources = SourceRegistry.for_agent("u", "a", data_dir=Path(directory.name) / "data")
        self.files = self.sources.agent_files()

    def test_soul_write_limit_follows_settings(self) -> None:
        with _limits(soul_chars=1000):
            self.files.write_soul("x" * 1000)
            with self.assertRaises(InputError):
                self.files.write_soul("x" * 1001)
        with _limits(soul_chars=20000):
            self.assertTrue(self.files.write_soul("y" * 16000))

    def test_prompt_counters_follow_settings(self) -> None:
        self.files.write_memory_text("memory", "Likes tea")
        self.files.write_memory_text("user", "User A")
        with _limits(memory_chars=300, user_chars=400):
            prompt = system_prompt(load_profile(self.sources))
            self.assertEqual(AgentProfile().limits.memory_chars, 300)
        self.assertIn("/300 chars", prompt)
        self.assertIn("/400 chars", prompt)
        self.assertIn("/2500 chars", system_prompt(load_profile(self.sources)))

    def test_memory_tool_limit_follows_settings(self) -> None:
        from types import SimpleNamespace

        from src.agent.tools import build_tools
        from src.config.levels import Level

        host = SimpleNamespace(sources=self.sources, config=SimpleNamespace(level=Level.ADMIN), index=None)
        with _limits(memory_chars=100):
            memory = build_tools(host, None, ["self"])[1]["memory"]
            self.assertTrue(memory(target="memory", action="add", content="a" * 90).startswith("saved"))
            self.assertNotIn("saved", memory(target="memory", action="add", content="b" * 90)[:5])


if __name__ == "__main__":
    unittest.main()
