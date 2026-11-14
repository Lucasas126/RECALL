from __future__ import annotations

import unittest

import test_user_preferences as preferences_tests

from src.config.field_defaults import agent_defaults, field_spec
from src.config.models import AgentTerminalConfig, ChatConfig, DreamConfig
from src.config.preferences import UserPreferences
from src.config.settings import ASSOC, ORCHESTRATOR
from src.core.sources.conversations import DEFAULT_DISPOSABLE_RETENTION_DAYS


class FieldDefaultsTests(unittest.TestCase):
    def test_specs_come_from_the_models(self) -> None:
        defaults = agent_defaults()
        for section, model in (("chat_config", ChatConfig), ("dream", DreamConfig), ("terminal", AgentTerminalConfig)):
            for name, spec in defaults[section].items():
                with self.subTest(field=f"{section}.{name}"):
                    info = model.model_fields[name]
                    self.assertEqual(spec["default"], info.default)
                    self.assertEqual(spec, field_spec(model, name))
        self.assertEqual(defaults["dream"]["max_steps"], {"default": 60, "min": 1, "max": 500})
        self.assertEqual(defaults["dream"]["max_tokens"], {"default": 500_000, "min": 1000, "max": 10_000_000})
        self.assertEqual(defaults["dream"]["max_minutes"], {"default": 5, "min": 1, "max": 120})
        self.assertNotIn("conversation_messages_retained", defaults["chat_config"])
        self.assertEqual(defaults["compaction"]["trigger"], {"default": 50, "min": 1, "max": 10_000_000})
        self.assertEqual(defaults["compaction"]["model"]["default"], "claude_subscription:haiku")
        self.assertEqual(defaults["terminal"]["timeout_seconds"], {"default": 60, "min": 1, "max": 600})

    def test_bounds_are_enforced_by_the_models(self) -> None:
        for model, name, valid, invalid in (
            (DreamConfig, "max_steps", (1, 500), (0, 501)),
            (DreamConfig, "every_hours", (1, 720), (0, 721)),
            (ChatConfig, "reasoning_tokens", (1,), (0,)),
        ):
            for value in valid:
                model.model_validate({name: value})
            for value in invalid:
                with self.subTest(field=name, value=value), self.assertRaises(ValueError):
                    model.model_validate({name: value})


class PreferencesDefaultsWebTests(unittest.TestCase):
    setUp = preferences_tests.UserPreferencesWebTests.setUp

    def test_preferences_carry_the_effective_defaults_and_bounds(self) -> None:
        defaults = self.client.get(self.url).json()["defaults"]
        self.assertEqual(defaults["associative_max_matches"], {"default": ASSOC.top_n, "min": 1, "max": 20})
        self.assertEqual(defaults["disposable_retention_days"], {"default": DEFAULT_DISPOSABLE_RETENTION_DAYS, "min": 1, "max": 365})
        self.assertEqual(defaults["orchestrator"], ORCHESTRATOR.enabled)
        self.assertEqual(defaults["language"], "es")  # user-one is configured in es-ES
        self.assertEqual((defaults["theme"], defaults["show_thinking"], defaults["core_agent_id"]), ("system", True, None))
        self.assertIs(defaults["show_agent_avatar"], True)
        self.assertIn("default_agent_id", defaults)  # nullable: null means the first agent
        self.assertIsNone(defaults["default_agent_id"])
        self.assertEqual(defaults["default_llm_model"], "openai/gpt-4o-mini")
        self.assertEqual([agent["id"] for agent in defaults["agents"]], ["agent-a", "agent-b"])

    def test_bounds_match_the_preferences_model(self) -> None:
        defaults = self.client.get(self.url).json()["defaults"]
        for key in ("associative_max_matches", "disposable_retention_days"):
            spec = defaults[key]
            UserPreferences.model_validate({key: spec["min"]})
            UserPreferences.model_validate({key: spec["max"]})
            for bad in (spec["min"] - 1, spec["max"] + 1):
                with self.subTest(key=key, value=bad), self.assertRaises(ValueError):
                    UserPreferences.model_validate({key: bad})

    def test_saving_null_resets_a_setting_to_its_default(self) -> None:
        saved = self.client.patch(self.url, json={"disposable_retention_days": 7, "default_llm_model": None}).json()
        self.assertEqual(saved["disposable_retention_days"], 7)
        self.assertEqual(saved["default_llm_model"], "openai/gpt-4o-mini")
        reset = self.client.patch(self.url, json={"disposable_retention_days": None}).json()
        self.assertIsNone(reset["disposable_retention_days"])
        self.assertNotIn('"disposable_retention_days": 7', self.path.read_text(encoding="utf-8"))

    def test_agent_defaults_endpoint(self) -> None:
        response = self.client.get("/api/agents/defaults")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), agent_defaults())
        self.client.cookies.clear()
        self.assertEqual(self.client.get("/api/agents/defaults").status_code, 401)
