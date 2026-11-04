from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path
from string import Formatter
from unittest.mock import patch

import test_service_steering as steering
import test_user_preferences as preferences_tests
from provider_helpers import isolate_providers

from src.config.preferences import UserPreferences
from src.utils.i18n import InputError, UserMessageError, error_message, messages


class LocalizationCatalogTests(unittest.TestCase):
    def test_translation_templates_preserve_format_fields(self) -> None:
        formatter = Formatter()
        for english, spanish in messages("es").items():
            with self.subTest(template=english):
                fields = lambda text: {name for _, name, _, _ in formatter.parse(text) if name is not None}
                self.assertEqual(fields(english), fields(spanish))

    def test_opaque_provider_text_and_interpolated_values_are_not_translated(self) -> None:
        original = "upstream error {raw}: message is empty"
        self.assertEqual(error_message("es", RuntimeError(original)), original)
        error = UserMessageError("Generation failed: {error}", error=original)
        self.assertEqual(error_message("es", error), f"La generación ha fallado: {original}")
        self.assertEqual(error_message("en", error), f"Generation failed: {original}")

    def test_every_registered_python_channel_template_has_a_translation(self) -> None:
        root = Path(__file__).resolve().parents[1]
        catalog = messages("es")
        localized_calls = {
            "InputError", "UnknownUser", "UnknownAgent", "UnknownChat", "AgentBusy",
            "PreferencesError", "FavoritesError", "CatalogUnavailable", "ChatMetadataError",
            "McpError", "McpNotConnected", "UnknownMcpServer",
        }
        for relative in (
            "src/telegram/bot.py", "src/service/chat.py", "src/config/preferences.py",
            "src/config/favorites.py", "src/providers/catalog.py", "src/core/sources/conversations.py",
            "src/providers/mcp.py", "src/service/providers.py",
        ):
            path = root.joinpath(*relative.split("/"))
            for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
                if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Name):
                    continue
                name = node.func.id
                index = 1 if name in {"message", "_msg"} else 0
                if name not in localized_calls | {"message", "_msg"} or len(node.args) <= index:
                    continue
                template = node.args[index]
                if isinstance(template, ast.Constant) and isinstance(template.value, str):
                    with self.subTest(file=relative, line=node.lineno):
                        self.assertIn(template.value, catalog)

    def test_every_literal_browser_template_has_a_translation(self) -> None:
        path = Path(__file__).resolve().parents[1] / "src" / "web" / "static" / "app.js"
        templates = re.findall(r'\btr\("((?:[^"\\]|\\.)*)"', path.read_text(encoding="utf-8"))
        for template in templates:
            template = template.replace('\\"', '"')
            with self.subTest(template=template):
                self.assertIn(template, messages("es"))


class LocalizationWebTests(unittest.TestCase):
    setUp = preferences_tests.UserPreferencesWebTests.setUp

    def test_public_catalog_supports_only_en_and_es(self) -> None:
        spanish = self.client.get("/api/localization?language=es")
        self.assertEqual(spanish.status_code, 200)
        self.assertEqual(spanish.json()["Settings"], "Ajustes")
        self.assertEqual(self.client.get("/api/localization?language=en").json()["Settings"], "Settings")
        self.assertEqual(self.client.get("/api/localization?language=fr").status_code, 422)

    def test_saved_language_overrides_request_header_for_application_errors(self) -> None:
        self.client.headers["Accept-Language"] = "en"
        response = self.client.patch("/api/preferences", json={"agent_id": "foreign"})
        self.assertEqual(response.status_code, 404)
        self.assertIn("no pertenece", response.json()["detail"])
        self.client.patch("/api/preferences", json={"language": "en"})
        self.client.headers["Accept-Language"] = "es"
        response = self.client.patch("/api/preferences", json={"agent_id": "foreign"})
        self.assertIn("not owned", response.json()["detail"])

    def test_service_input_errors_are_localized_without_changing_http_status(self) -> None:
        self.users["user-one"].provider.name = "ollama"
        isolate_providers(self, openrouter_key=None)
        response = self.client.get("/api/speech-to-text/models")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Proveedor no disponible: OpenRouter", response.json()["detail"])
        self.assertIn("no está definida", response.json()["detail"])

    def test_error_language_remains_available_if_saved_agent_was_removed(self) -> None:
        self.client.patch("/api/preferences", json={"agent_id": "agent-b", "language": "es"})
        self.users["user-one"].agents.remove("agent-b")
        response = self.client.get("/api/preferences")
        self.assertEqual(response.status_code, 404)
        self.assertIn("no pertenece", response.json()["detail"])

    def test_corrupt_storage_uses_last_known_request_language_and_reports_failure(self) -> None:
        self.path.write_text("invalid", encoding="utf-8")
        self.client.headers["Accept-Language"] = "es"
        with self.assertLogs("recall.web", level="WARNING"):
            response = self.client.get("/api/preferences")
        self.assertEqual(response.status_code, 500)
        self.assertIn("No se han podido leer los ajustes", response.json()["detail"])

    def test_sse_localizes_application_errors_but_not_provider_text(self) -> None:
        for error, expected in (
            (InputError("message is empty"), "El mensaje está vacío."),
            (RuntimeError("ORIGINAL vendor failure {raw}"), "ORIGINAL vendor failure {raw}"),
        ):
            def failing_stream():
                raise error
                yield

            with self.subTest(error=error), patch.object(self.service, "send", return_value=failing_stream()):
                with self.assertLogs("recall.web", level="ERROR"):
                    response = self.client.post(
                        "/api/agents/agent-a/chats/test-chat/messages", json={"content": "Hello"},
                    )
                self.assertEqual(response.status_code, 200)
                self.assertIn(expected, response.text)
                self.assertIn("event: done", response.text)


class SharedLanguageGenerationTests(steering.SteeringFixture):
    def test_cached_agent_receives_latest_user_language_without_being_rebuilt(self) -> None:
        self.service.update_preferences("user-one", UserPreferences(language="es"))
        list(self.service.send("user-one", "agent-one", self.chat, "First"))
        agent = self.agents[("user-one", "agent-one")]
        self.assertEqual(agent.config.language, "es")
        self.service.update_preferences("user-one", UserPreferences(language="en"))
        list(self.service.send("user-one", "agent-one", self.chat, "Second"))
        self.assertIs(self.agents[("user-one", "agent-one")], agent)
        self.assertEqual(agent.config.language, "en")


if __name__ == "__main__":
    unittest.main()
