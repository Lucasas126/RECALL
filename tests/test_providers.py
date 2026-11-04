from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from auth_helpers import TEST_PASSWORD_HASH, login
from provider_helpers import CLAUDE_LOGGED_IN, CLAUDE_LOGGED_OUT, CLAUDE_NOT_FOUND, isolate_providers
from src.config.model_refs import model_name, prefix_of, reference, split_reference
from src.config.models import ClaudeSubscriptionConfig, DreamConfig, OllamaConfig, OpenRouterConfig, UserConfig
from src.core.sources.conversations import ChatModel
from src.providers.catalog import OpenRouterModel, OpenRouterTranscriptionModel
from src.providers.claude_subscription import ClaudeCodeLoggedOut, ClaudeCodeMissing
from src.providers.claude_subscription.status import cli_status
from src.providers.ollama import OllamaUnavailable
from src.providers.provider import ProviderUnavailable
from src.providers.routing import provider_for
from src.service.chat import ChatService
from src.service.providers import ProviderAvailability, canonical_reference, provider_rows
from src.utils.i18n import InputError
from src.web.app import create_app

SECRET = "sk-or-v1-never-shown-0123456789"
USER_YAML = f"""# user file kept by hand
id: user-one
name: User One
password_hash: '{TEST_PASSWORD_HASH}'
provider:
  name: openrouter
  api_key: OPENROUTER_TEST_KEY   # the main key
agents: [agent-a]

models:
  chat: vendor/alpha
"""


class ModelReferenceTests(unittest.TestCase):
    def test_only_known_provider_names_are_prefixes(self) -> None:
        self.assertEqual(split_reference("ollama:llama3:8b", "openrouter"), ("ollama", "llama3:8b"))
        self.assertEqual(split_reference("llama3:8b", "ollama"), ("ollama", "llama3:8b"))
        self.assertEqual(split_reference("vendor/model:free", "openrouter"), ("openrouter", "vendor/model:free"))
        self.assertEqual(split_reference("openrouter:vendor/a", "ollama"), ("openrouter", "vendor/a"))
        self.assertIsNone(prefix_of("vendor/model:free"))
        self.assertEqual(model_name("claude_subscription:sonnet"), "sonnet")
        self.assertEqual(reference("openrouter", "vendor/a", "openrouter"), "vendor/a")
        self.assertEqual(reference("openrouter", "vendor/a", "ollama"), "openrouter:vendor/a")

    def test_chat_model_accepts_ollama_and_bare_ids_of_any_main(self) -> None:
        self.assertEqual(ChatModel(provider="ollama", id="llama3:8b").id, "llama3:8b")
        self.assertEqual(ChatModel(provider="ollama", id="ollama:qwen3").provider, "ollama")
        with self.assertRaises(ValueError):
            ChatModel(provider="openrouter", id="ollama:qwen3")


class RoutingTests(unittest.TestCase):
    def setUp(self) -> None:
        from provider_helpers import allow_every_default_login
        allow_every_default_login(self)

    def settings(self, **changes) -> dict:
        return {"main": "openrouter", "main_api_key": "main-key", "openrouter": OpenRouterConfig(),
                "ollama": OllamaConfig(), "claude": ClaudeSubscriptionConfig()} | changes

    def test_any_provider_can_be_routed_without_an_enabled_switch(self) -> None:
        provider = provider_for("ollama", **self.settings(ollama=OllamaConfig(host="http://box:11434")))
        self.assertEqual(provider.base_url, "http://box:11434")
        self.assertEqual(provider.own_model("ollama:llama3:8b"), "llama3:8b")
        self.assertEqual(provider_for("claude_subscription", user_id="u", **self.settings()).name, "claude_subscription")
        from src.providers.ollama import is_ollama_cloud
        self.assertTrue(is_ollama_cloud("ollama:glm-4.7:cloud"))
        self.assertTrue(is_ollama_cloud("gpt-oss:120b-cloud"))
        self.assertFalse(is_ollama_cloud("llama3:8b"))

    def test_openrouter_beside_another_main_reads_its_own_key_env_var(self) -> None:
        settings = self.settings(main="ollama", openrouter=OpenRouterConfig(api_key="SECOND_KEY"))
        with patch.dict(os.environ, {"SECOND_KEY": ""}):
            with self.assertRaisesRegex(ProviderUnavailable, r"provider not available: OpenRouter: API key variable 'SECOND_KEY' is not set"):
                provider_for("openrouter", purpose="the model openrouter:vendor/a", **settings)
        with patch.dict(os.environ, {"SECOND_KEY": "value"}):
            self.assertEqual(provider_for("openrouter", **settings).api_key, "value")

    def test_claude_code_failures_are_provider_unavailable_errors(self) -> None:
        self.assertTrue(issubclass(ClaudeCodeMissing, ProviderUnavailable))
        self.assertTrue(issubclass(ClaudeCodeLoggedOut, ProviderUnavailable))


def _status_run(outputs: dict):
    def run(command, env, timeout):
        key = "version" if command[-1] == "--version" else "auth"
        code, out = outputs[key]
        return subprocess.CompletedProcess(command, code, stdout=out, stderr="")
    return run


class ClaudeStatusTests(unittest.TestCase):
    def setUp(self) -> None:
        env = {k: v for k, v in os.environ.items() if not k.startswith(("ANTHROPIC_", "CLAUDE_CODE_USE_"))}
        patcher = patch.dict(os.environ, env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.settings = ClaudeSubscriptionConfig(command=sys.executable)

    def test_logged_in_status_never_carries_account_identifiers(self) -> None:
        auth = '{"loggedIn": true, "authMethod": "claude.ai", "email": "someone@example.com", "orgName": "Org", "subscriptionType": "pro"}'
        with patch("src.providers.claude_subscription.status._run", side_effect=_status_run(
                {"version": (0, "2.1.316 (Claude Code)\n"), "auth": (0, auth)})):
            status = cli_status(self.settings)
        self.assertEqual(status, {"cli_found": True, "command": sys.executable, "version": "2.1.316",
                                  "logged_in": True, "hint": None, "auth_method": "claude.ai", "subscription": "pro"})
        self.assertNotIn("example.com", repr(status))

    def test_logged_out_status_shows_the_login_hint(self) -> None:
        with patch("src.providers.claude_subscription.status._run", side_effect=_status_run(
                {"version": (0, "2.1.316 (Claude Code)"), "auth": (1, '{"loggedIn": false}')})):
            status = cli_status(self.settings.model_copy(update={"claude_config_dir": "/logins/me"}))
        self.assertFalse(status["logged_in"])
        self.assertIn("claude auth login", status["hint"])
        self.assertIn("CLAUDE_CONFIG_DIR=/logins/me", status["hint"])

    def test_missing_cli_and_conflicting_env(self) -> None:
        status = cli_status(ClaudeSubscriptionConfig(command=str(Path(tempfile.gettempdir()) / "no-such-claude.exe")))
        self.assertFalse(status["cli_found"])
        self.assertIn("npm install -g @anthropic-ai/claude-code", status["hint"])
        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": SECRET}):
            status = cli_status(self.settings)
        self.assertIn("ANTHROPIC_API_KEY", status["hint"])
        self.assertNotIn(SECRET, repr(status))

    def test_unreadable_auth_answer_is_reported_not_guessed(self) -> None:
        with patch("src.providers.claude_subscription.status._run", side_effect=_status_run(
                {"version": (0, "2.1.316 (Claude Code)"), "auth": (0, "Not JSON")})):
            status = cli_status(self.settings)
        self.assertIsNone(status["logged_in"])
        self.assertIn("claude auth status", status["hint"])


class ProvidersApiTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.config_dir, self.data_dir = Path(tmp.name) / "config", Path(tmp.name) / "data"
        (self.config_dir / "user-one" / "agents").mkdir(parents=True)
        self.user_file = self.config_dir / "user-one" / "user.yaml"
        self.user_file.write_text(USER_YAML, encoding="utf-8")
        (self.config_dir / "user-one" / "agents" / "agent-a.yaml").write_text(
            "id: agent-a\nname: Agent A\n", encoding="utf-8")
        for target in ("src.service.chat.CONFIG_DIR", "src.config.loader.CONFIG_DIR"):
            patcher = patch(target, self.config_dir)
            patcher.start()
            self.addCleanup(patcher.stop)
        for target in ("src.service.chat.DATA_DIR", "src.config.loader.DATA_DIR", "src.core.sources.registry.DATA_DIR"):
            patcher = patch(target, self.data_dir)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.mocks = isolate_providers(
            self, openrouter_key=SECRET, variable="OPENROUTER_TEST_KEY", claude=CLAUDE_LOGGED_OUT, ollama=["llama3:8b"])
        openrouter = patch("src.service.providers.list_openrouter_models", side_effect=lambda q="", *, zdr=False: [
            m for m in [OpenRouterModel(id="vendor/alpha", name="Alpha", description="", zdr_providers=["P"]),
                        OpenRouterModel(id="vendor/open", name="Open", description="", zdr_providers=[])]
            if (not zdr or m.zdr_providers) and q.casefold() in m.id])
        openrouter.start()
        self.addCleanup(openrouter.stop)
        stt = patch("src.service.providers.list_openrouter_transcription_models", return_value=[
            OpenRouterTranscriptionModel(id="openai/whisper", name="Whisper", description="", zdr_providers=["P"])])
        stt.start()
        self.addCleanup(stt.stop)
        self.service = ChatService(agent_factory=Mock(), dream=Mock())
        self.client = TestClient(create_app(self.service, dream_scheduler=False))
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        login(self.client, "user-one")

    def status(self, capability: str = "chat") -> dict:
        response = self.client.get("/api/providers/status", params={"capability": capability})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotIn(SECRET, response.text)
        return {row["id"]: row for row in response.json()["providers"]}

    def test_availability_names_what_is_usable_and_never_returns_secrets(self) -> None:
        rows = self.status()
        self.assertEqual(list(rows), ["openrouter", "ollama", "claude_subscription"])
        self.assertTrue(rows["openrouter"]["available"])
        self.assertTrue(rows["openrouter"]["main"])
        self.assertEqual(rows["openrouter"]["detail"], "OPENROUTER_TEST_KEY")  # the variable name, not its value
        self.assertTrue(rows["openrouter"]["zdr_applies"])
        self.assertTrue(rows["ollama"]["available"])
        self.assertFalse(rows["claude_subscription"]["available"])
        self.assertEqual(rows["claude_subscription"]["reason"], "logged_out")
        self.assertIn("claude auth login", rows["claude_subscription"]["detail"])
        self.assertTrue(rows["claude_subscription"]["experimental"])
        # speech-to-text is served by OpenRouter only
        self.assertEqual(list(self.status("stt")), ["openrouter"])
        self.assertEqual(self.client.get("/api/providers/status", params={"capability": "other"}).status_code, 422)
        self.client.cookies.clear()
        self.assertEqual(self.client.get("/api/providers/status").status_code, 401)

    def test_every_unavailable_reason_is_reported(self) -> None:
        self.service._availability.forget()
        self.mocks["ollama"].side_effect = OllamaUnavailable("Ollama at http://x did not answer: ConnectError")
        self.mocks["claude"].return_value = CLAUDE_NOT_FOUND
        with patch.dict(os.environ):
            os.environ.pop("OPENROUTER_TEST_KEY")
            rows = self.status()
        self.assertEqual([(r["id"], r["available"], r["reason"]) for r in rows.values()], [
            ("openrouter", False, "no_api_key"), ("ollama", False, "unreachable"), ("claude_subscription", False, "cli_missing")])
        self.assertTrue(rows["ollama"]["detail"].startswith("http"))
        self.service._availability.forget()
        self.mocks["claude"].return_value = CLAUDE_LOGGED_OUT | {"logged_in": None}
        self.assertEqual(self.status()["claude_subscription"]["reason"], "cli_unknown")

    def test_slow_checks_are_cached_for_a_minute_but_the_key_check_is_live(self) -> None:
        now = [0.0]
        availability = ProviderAvailability(ttl=60.0, clock=lambda: now[0])
        user = UserConfig(id="u", name="U", provider={"name": "openrouter", "api_key": "OPENROUTER_TEST_KEY"}, agents=[])
        availability.rows(user)
        availability.rows(user)
        self.assertEqual((self.mocks["claude"].call_count, self.mocks["ollama"].call_count), (1, 1))
        with patch.dict(os.environ):
            os.environ.pop("OPENROUTER_TEST_KEY")
            self.assertFalse(availability.rows(user)[0]["available"])  # not cached: a new key shows at once
        now[0] = 61.0
        availability.rows(user)
        self.assertEqual((self.mocks["claude"].call_count, self.mocks["ollama"].call_count), (2, 2))

    def test_catalog_lists_only_the_requested_provider_per_target(self) -> None:
        self.mocks["claude"].return_value = CLAUDE_LOGGED_IN
        self.service._availability.forget()

        def ids(**params) -> list[tuple[str, str]]:
            response = self.client.get("/api/models/catalog", params=params)
            self.assertEqual(response.status_code, 200, response.text)
            return [(m["id"], m["provider"]) for m in response.json()["models"]]

        # ZDR is not required by default: the plain catalog; asking for it narrows OpenRouter only
        self.assertEqual(ids(provider="openrouter"), [("vendor/alpha", "openrouter"), ("vendor/open", "openrouter")])
        self.assertEqual(ids(provider="openrouter", zdr="true"), [("vendor/alpha", "openrouter")])
        self.assertEqual(ids(provider="ollama"), [("ollama:llama3:8b", "ollama")])
        self.assertEqual(ids(provider="claude_subscription"), [
            ("claude_subscription:haiku", "claude_subscription"), ("claude_subscription:sonnet", "claude_subscription"),
            ("claude_subscription:opus", "claude_subscription")])
        self.assertEqual(ids(provider="claude_subscription", q="opus"), [("claude_subscription:opus", "claude_subscription")])
        # the ZDR filter narrows OpenRouter only; Claude rows are never ZDR whatever the filter says
        self.assertIn(("vendor/open", "openrouter"), ids(provider="openrouter", zdr="false"))
        self.assertEqual(len(ids(provider="claude_subscription", zdr="true")), 3)
        claude_rows = self.client.get("/api/models/catalog", params={"provider": "claude_subscription"}).json()["models"]
        self.assertFalse(any(row["zdr_applies"] for row in claude_rows))
        # speech-to-text: OpenRouter only, with or without naming it
        self.assertEqual(ids(capability="stt"), [("openai/whisper", "openrouter")])
        self.assertEqual(ids(capability="stt", provider="openrouter"), [("openai/whisper", "openrouter")])
        for other in ("ollama", "claude_subscription"):
            refused = self.client.get("/api/models/catalog", params={"capability": "stt", "provider": other})
            self.assertEqual(refused.status_code, 400)
            self.assertIn("cannot serve this kind of model", refused.json()["detail"])
        self.assertEqual(self.client.get("/api/models/catalog", params={"provider": "nope"}).status_code, 400)

    def test_catalog_without_provider_merges_the_available_ones_for_the_any_tab(self) -> None:
        self.mocks["claude"].return_value = CLAUDE_LOGGED_IN
        self.service._availability.forget()

        def merged(**params) -> list[tuple[str, str]]:
            response = self.client.get("/api/models/catalog", params=params)
            self.assertEqual(response.status_code, 200, response.text)
            for row in response.json()["models"]:
                self.assertTrue(row["provider_label"])
            return [(m["id"], m["provider"]) for m in response.json()["models"]]

        everything = [("vendor/alpha", "openrouter"), ("vendor/open", "openrouter"), ("ollama:llama3:8b", "ollama"),
                      ("claude_subscription:haiku", "claude_subscription"),
                      ("claude_subscription:sonnet", "claude_subscription"), ("claude_subscription:opus", "claude_subscription")]
        self.assertEqual(merged(), everything)
        # ZDR narrows the OpenRouter rows only; Ollama and Claude rows stay
        self.assertEqual(merged(zdr="true"), everything[:1] + everything[2:])
        self.assertEqual(merged(q="opus"), [("claude_subscription:opus", "claude_subscription")])

    def test_any_skips_unavailable_providers_and_survives_one_failing_catalog(self) -> None:
        # Claude is logged out: skipped without an error
        self.service._availability.forget()
        response = self.client.get("/api/models/catalog")
        self.assertEqual({m["provider"] for m in response.json()["models"]}, {"openrouter", "ollama"})
        # Ollama answered the availability check, then its listing fails: the other providers still show
        self.client.get("/api/providers/status")
        self.mocks["ollama"].side_effect = OllamaUnavailable("down")
        response = self.client.get("/api/models/catalog")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual({m["provider"] for m in response.json()["models"]}, {"openrouter"})

    def test_an_unavailable_provider_is_refused_with_what_to_fix(self) -> None:
        response = self.client.get("/api/models/catalog", params={"provider": "claude_subscription"})
        self.assertEqual(response.status_code, 400)
        self.assertIn("Provider not available: Claude subscription", response.json()["detail"])
        self.assertIn("claude auth login", response.json()["detail"])
        self.mocks["ollama"].side_effect = OllamaUnavailable("down")
        self.service._availability.forget()
        response = self.client.get("/api/models/catalog", params={"provider": "ollama"})
        self.assertIn("Provider not available: Ollama", response.json()["detail"])

    def test_chosen_models_are_saved_as_provider_references(self) -> None:
        self.mocks["claude"].return_value = CLAUDE_LOGGED_IN
        self.service._availability.forget()

        def default_llm(model: str):
            return self.client.patch("/api/preferences", json={"default_llm_model": model})

        saved = default_llm("ollama:llama3:8b")
        self.assertEqual(saved.status_code, 200, saved.text)
        self.assertEqual(saved.json()["default_llm_model"], "ollama:llama3:8b")
        self.assertEqual(default_llm("claude_subscription:sonnet").json()["default_llm_model"], "claude_subscription:sonnet")
        # the main provider's models are stored bare, even when chosen with their prefix
        self.assertEqual(default_llm("openrouter:vendor/alpha").json()["default_llm_model"], "vendor/alpha")
        self.assertEqual(default_llm("claude_subscription:other").status_code, 400)
        self.assertEqual(default_llm("ollama:not-installed").status_code, 400)
        # nothing of the old per-user switches remains in user.yaml
        self.assertEqual(self.user_file.read_text(encoding="utf-8"), USER_YAML)
        stt = self.client.patch("/api/preferences", json={"default_stt_model": "ollama:whisper"})
        self.assertEqual(stt.status_code, 400)
        self.assertIn("cannot serve this kind of model", stt.json()["detail"])
        chat_id = self.service.create_chat("user-one", "agent-a")["id"]
        chosen = self.service.set_chat_model("user-one", "agent-a", chat_id, "ollama:llama3:8b", zdr=True)
        self.assertEqual((chosen["provider"], chosen["id"]), ("ollama", "ollama:llama3:8b"))
        chosen = self.service.set_chat_model("user-one", "agent-a", chat_id, "claude_subscription:opus", zdr=True)
        self.assertEqual((chosen["provider"], chosen["id"], chosen["zdr"]), ("claude_subscription", "claude_subscription:opus", False))

    def test_a_provider_that_becomes_unavailable_gives_an_actionable_error(self) -> None:
        self.mocks["claude"].return_value = CLAUDE_LOGGED_IN
        self.service._availability.forget()
        self.assertEqual(self.client.patch("/api/preferences", json={"default_llm_model": "claude_subscription:haiku"}).status_code, 200)
        chat_id = self.service.create_chat("user-one", "agent-a")["id"]
        # the login lapses; the next choice (and the dream tab) say what to do instead of failing later
        self.mocks["claude"].return_value = CLAUDE_LOGGED_OUT
        self.service._availability.forget()
        again = self.client.patch("/api/preferences", json={"default_llm_model": "claude_subscription:sonnet"})
        self.assertEqual(again.status_code, 400)
        self.assertIn("Provider not available: Claude subscription", again.json()["detail"])
        self.assertIn("`claude auth login`", again.json()["detail"])
        with self.assertRaisesRegex(InputError, "Provider not available: Claude subscription"):
            self.service.set_chat_model("user-one", "agent-a", chat_id, "claude_subscription:opus", zdr=True)
        with self.assertRaisesRegex(InputError, "Provider not available: Claude subscription"):
            self.service.update_dream_settings("user-one", "agent-a", DreamConfig(model="claude_subscription:sonnet"))
        # the Spanish UI gets the same fix in Spanish
        self.client.patch("/api/preferences", json={"language": "es"})
        spanish = self.client.patch("/api/preferences", json={"default_llm_model": "claude_subscription:sonnet"})
        self.assertIn("Proveedor no disponible: Claude subscription", spanish.json()["detail"])
        # and the saved default is untouched
        self.assertEqual(self.client.get("/api/preferences").json()["default_llm_model"], "claude_subscription:haiku")

    def test_the_provider_settings_endpoints_are_gone(self) -> None:
        for method in ("get", "put"):
            self.assertIn(getattr(self.client, method)("/api/providers").status_code, (404, 405))
        self.assertNotIn("providers", self.client.get("/api/me").json())
        self.assertEqual(self.client.get("/api/me").json()["provider"], "openrouter")


class UserConfigTests(unittest.TestCase):
    def test_there_is_no_enabled_key_and_claude_options_stay_optional(self) -> None:
        base = {"id": "u", "name": "U", "provider": {"name": "openrouter"}, "agents": []}
        for block in ("openrouter", "ollama", "claude_subscription"):
            with self.assertRaises(ValueError):
                UserConfig.model_validate(base | {block: {"enabled": True}})
        user = UserConfig.model_validate(base | {"claude_subscription": {"claude_config_dir": "/login", "timeout_seconds": 120}})
        self.assertEqual((user.claude_subscription.claude_config_dir, user.claude_subscription.timeout_seconds), ("/login", 120))
        self.assertEqual(user.openrouter_settings().api_key, "OPENROUTER_API_KEY")


class CatalogFunctionTests(unittest.TestCase):
    def test_main_ollama_rows_are_bare_and_openrouter_rows_prefixed(self) -> None:
        user = UserConfig(id="u", name="U", provider={"name": "ollama"}, agents=[])
        with patch("src.service.providers.list_ollama_models", return_value=["qwen3"]), \
                patch("src.service.providers.list_openrouter_models", return_value=[
                    OpenRouterModel(id="vendor/alpha", name="Alpha", description="", zdr_providers=["P"])]):
            ollama = provider_rows(user, "ollama", zdr=True, capability="chat")
            openrouter = provider_rows(user, "openrouter", zdr=True, capability="chat")
        self.assertEqual([r["id"] for r in ollama], ["qwen3"])
        self.assertEqual([r["id"] for r in openrouter], ["openrouter:vendor/alpha"])
        self.assertEqual(provider_rows(user, "ollama", zdr=True, capability="stt"), [])
        self.assertEqual(canonical_reference(user, "ollama:qwen3"), ("ollama", "qwen3"))
        self.assertEqual(canonical_reference(user, "vendor/alpha"), ("ollama", "vendor/alpha"))
        self.assertEqual(canonical_reference(user, "openrouter:vendor/alpha"), ("openrouter", "openrouter:vendor/alpha"))


if __name__ == "__main__":
    unittest.main()
