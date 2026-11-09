from __future__ import annotations

import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from provider_helpers import CLAUDE_LOGGED_IN, isolate_providers, set_default_login_users
from src.agent.agent import named_provider
from src.config.models import ClaudeLoginSettings, ClaudeSubscriptionConfig, ModelsConfig, ResolvedAgent, UserConfig
from src.providers.claude_subscription import claude_subscription_provider, ClaudeSubscriptionProvider
from src.providers.claude_subscription.login import login_allowed
from src.providers.routing import provider_for
from src.service.chat import ChatService
from src.service.providers import ProviderAvailability, unavailable
from src.utils.i18n import InputError, error_code, error_message

_ROOT = Path(__file__).resolve().parents[1]


def _user(user_id: str, **claude) -> UserConfig:
    return UserConfig(id=user_id, name=user_id.title(), provider={"name": "openrouter"}, agents=["a"],
                      claude_subscription=claude)


def _resolved(user_id: str, **claude) -> ResolvedAgent:
    return ResolvedAgent(
        user_id=user_id, id="a", name="A", models=ModelsConfig(), provider_name="openrouter", api_key="k",
        embeddings_provider_name="openrouter", embeddings_api_key="k",
        claude_subscription=ClaudeSubscriptionConfig(**claude),
    )


class LoginRuleTests(unittest.TestCase):
    def test_owner_own_folder_stranger_and_empty_list(self) -> None:
        set_default_login_users(self, ["owner"])
        self.assertTrue(login_allowed("owner", ClaudeSubscriptionConfig()))
        self.assertFalse(login_allowed("guest", ClaudeSubscriptionConfig()))
        self.assertTrue(login_allowed("guest", ClaudeSubscriptionConfig(claude_config_dir="claude-login")))
        self.assertFalse(login_allowed(None, ClaudeSubscriptionConfig()))  # fails closed
        set_default_login_users(self, [])
        self.assertFalse(login_allowed("owner", ClaudeSubscriptionConfig()))
        self.assertTrue(login_allowed("owner", ClaudeSubscriptionConfig(claude_config_dir="claude-login")))

    def test_the_setting_rejects_unknown_keys_and_blank_ids(self) -> None:
        self.assertEqual(ClaudeLoginSettings().default_login_users, [])
        self.assertEqual(ClaudeLoginSettings(default_login_users=[" a "]).default_login_users, ["a"])
        with self.assertRaises(ValueError):
            ClaudeLoginSettings(default_login_users=[""])
        with self.assertRaises(ValueError):
            ClaudeLoginSettings(owner="a")


class PickerStatusTests(unittest.TestCase):
    def reasons(self, *users: UserConfig) -> list[tuple]:
        availability = ProviderAvailability()
        return [next((row["available"], row["reason"]) for row in availability.rows(user) if row["id"] == "claude_subscription")
                for user in users]

    def test_non_allowed_user_sees_the_coded_reason_without_starting_the_cli(self) -> None:
        mocks = isolate_providers(self, claude=CLAUDE_LOGGED_IN, default_login_users=["owner"])
        owner, guest, own = _user("owner"), _user("guest"), _user("user-b", claude_config_dir="claude-login")
        self.assertEqual(self.reasons(owner, guest, own), [(True, None), (False, "no_own_login"), (True, None)])
        self.assertEqual(mocks["claude"].call_count, 2)  # the guest never reached the CLI check
        mocks["claude"].reset_mock()
        self.reasons(guest)
        mocks["claude"].assert_not_called()

    def test_empty_list_means_nobody_uses_the_default_login(self) -> None:
        isolate_providers(self, claude=CLAUDE_LOGGED_IN, default_login_users=[])
        self.assertEqual(self.reasons(_user("owner"), _user("user-b", claude_config_dir="claude-login")),
                         [(False, "no_own_login"), (True, None)])

    def test_the_cache_does_not_leak_the_owners_result_to_another_user(self) -> None:
        isolate_providers(self, claude=CLAUDE_LOGGED_IN, default_login_users=["owner"])
        availability = ProviderAvailability()
        statuses = [next(r for r in availability.rows(u) if r["id"] == "claude_subscription")["available"]
                    for u in (_user("owner"), _user("guest"))]
        self.assertEqual(statuses, [True, False])

    def test_the_error_is_coded_and_localized(self) -> None:
        error = unavailable({"id": "claude_subscription", "reason": "no_own_login", "detail": None})
        self.assertEqual(error_code(error), "claude_no_own_login")
        self.assertIn("administrator", str(error))
        self.assertIn("administrador", error_message("es", error))


class RuntimeRefusalTests(unittest.TestCase):
    def setUp(self) -> None:
        set_default_login_users(self, ["owner"])

    def test_factory_refuses_before_creating_a_provider_or_process(self) -> None:
        with patch.object(ClaudeSubscriptionProvider, "__init__", side_effect=AssertionError("provider created")), \
                patch.object(subprocess, "Popen", side_effect=AssertionError("process spawned")):
            with self.assertRaises(InputError) as caught:
                claude_subscription_provider(ClaudeSubscriptionConfig(), user_id="guest")
        self.assertEqual(error_code(caught.exception), "claude_no_own_login")

    def test_factory_allows_the_owner_and_an_own_folder(self) -> None:
        self.assertIsInstance(claude_subscription_provider(ClaudeSubscriptionConfig(), user_id="owner"), ClaudeSubscriptionProvider)
        provider = claude_subscription_provider(ClaudeSubscriptionConfig(claude_config_dir="claude-login"), user_id="guest")
        self.assertTrue(provider.config_dir.endswith("/guest/claude-login"))

    def test_chat_dream_and_associative_route_through_the_same_guard(self) -> None:
        # Agent.provider_named / claude_provider (chat, dream, associative query) all end in named_provider
        with self.assertRaises(InputError) as caught:
            named_provider(_resolved("guest"), "claude_subscription", "the model claude_subscription:sonnet")
        self.assertEqual(error_code(caught.exception), "claude_no_own_login")
        self.assertIsInstance(named_provider(_resolved("owner"), "claude_subscription", "x"), ClaudeSubscriptionProvider)
        self.assertIsInstance(named_provider(_resolved("user-b", claude_config_dir="claude-login"), "claude_subscription", "x"),
                              ClaudeSubscriptionProvider)

    def test_routing_without_a_user_id_fails_closed(self) -> None:
        from src.config.models import OllamaConfig, OpenRouterConfig
        settings = {"main": "openrouter", "main_api_key": "k", "openrouter": OpenRouterConfig(), "ollama": OllamaConfig(),
                    "claude": ClaudeSubscriptionConfig()}
        with self.assertRaises(InputError):
            provider_for("claude_subscription", **settings)

    def test_chat_naming_by_a_non_allowed_user_never_reaches_the_cli(self) -> None:
        import src.service.chat as chat
        from src.config.models import ChatNamingConfig
        service = ChatService(agent_factory=Mock(), dream=Mock())
        conversations = Mock()
        conversations.generated_title.return_value = None
        naming = ChatNamingConfig(model="claude_subscription:haiku", min_messages=1)
        with patch.object(service, "_conversations", return_value=conversations), \
                patch.object(service, "_load", return_value=[{"role": "user", "content": "hi"}]), \
                patch.object(service, "_user", return_value=_user("guest")), patch.object(chat, "CHAT_NAMING", naming), \
                patch.object(subprocess, "Popen", side_effect=AssertionError("process spawned")):
            service._name_chat("guest", "a", "c")  # naming failures are logged, never raised
        conversations.set_generated_title.assert_not_called()


class TelegramAvailabilityTests(unittest.TestCase):
    def test_merged_model_list_omits_claude_for_a_non_allowed_user(self) -> None:
        isolate_providers(self, claude=CLAUDE_LOGGED_IN, ollama=[], default_login_users=["owner"])
        service = ChatService(agent_factory=Mock(), dream=Mock())
        shown: dict[str, list[str]] = {}
        for user in (_user("owner"), _user("guest"), _user("user-b", claude_config_dir="claude-login")):
            with patch.object(service, "_user", return_value=user), \
                    patch("src.service.chat.provider_settings.provider_rows",
                          side_effect=lambda u, provider, *a, **k: [{"id": f"{provider}:m", "provider": provider}]):
                shown[user.id] = [row["provider"] for row in service.model_catalog(user.id)["models"]]
        self.assertIn("claude_subscription", shown["owner"])
        self.assertNotIn("claude_subscription", shown["guest"])
        self.assertIn("claude_subscription", shown["user-b"])


class SettingsLoadingTests(unittest.TestCase):
    def _loaded(self, env_value: str | None) -> list[str]:
        env = {k: v for k, v in os.environ.items() if k != "RECALL_CLAUDE_DEFAULT_LOGIN_USERS"}
        if env_value is not None:
            env["RECALL_CLAUDE_DEFAULT_LOGIN_USERS"] = env_value
        result = subprocess.run(
            [sys.executable, "-c", "from src.config.settings import CLAUDE_LOGIN; print(','.join(CLAUDE_LOGIN.default_login_users))"],
            cwd=_ROOT, env=env, capture_output=True, text=True, check=True)
        return [part for part in result.stdout.strip().split(",") if part]

    def test_tracked_default_is_empty_and_the_environment_overrides_it(self) -> None:
        self.assertEqual(self._loaded(None), [])
        self.assertEqual(self._loaded("a, b"), ["a", "b"])
        self.assertEqual(self._loaded("  "), [])


if __name__ == "__main__":
    unittest.main()
