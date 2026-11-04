from __future__ import annotations

import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

import test_chat_models as models_tests
from src.config.models import AgentConfig, ModelsConfig
from src.config.preferences import UserPreferences
from src.providers.catalog import CatalogUnavailable
from src.service.chat import UnknownChat
from src.web.app import create_app


class UserModelDefaultsTests(models_tests.ModelFixture):
    def test_model_metadata_endpoint_is_user_scoped_and_does_not_read_history(self) -> None:
        source = self.sources[("user-one", "agent-one")]
        source.set_model(self.chat, "vendor/beta", zdr=False)
        with TestClient(create_app(self.service), raise_server_exceptions=False) as client:
            self.assertEqual(client.post(
                "/api/login", json={"user_id": "user-one", "password": "test-password-123"},
            ).status_code, 200)
            with patch.object(source, "load", side_effect=AssertionError("History must not be read")):
                with patch.object(self.service._preferences, "get", wraps=self.service._preferences.get) as get:
                    response = client.get(f"/api/agents/agent-one/chats/{self.chat}/models")
                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.json(), {
                        "model": {"id": "vendor/beta", "provider": "openrouter", "zdr": False},
                        "model_override": {"id": "vendor/beta", "provider": "openrouter", "zdr": False},
                        "stt_model": "openai/whisper-large-v3-turbo",
                        "reasoning_effort": {
                            "override": None, "default": "medium", "effective": "medium",
                            "reasoning": False, "supported": False,
                        },
                    })
                    get.assert_called_once_with("user-one")
            self.assertEqual(client.post(
                "/api/login", json={"user_id": "user-two", "password": "test-password-123"},
            ).status_code, 200)
            self.assertEqual(client.get(f"/api/agents/agent-one/chats/{self.chat}/models").status_code, 404)
            self.assertEqual(client.get(f"/api/agents/agent-two/chats/{self.chat}/models").status_code, 404)
        self.assertFalse(self.agents)

    def test_new_chat_overrides_are_null_and_defaults_follow_the_user(self) -> None:
        chat = self.service.get_chat("user-one", "agent-one", self.chat)
        self.assertIsNone(chat["model_override"])
        self.service.update_preferences("user-one", UserPreferences(default_llm_model="vendor/alpha"))
        self.assertEqual(self.service.get_chat("user-one", "agent-one", self.chat)["model"]["id"], "vendor/alpha")
        other = self.service.create_chat("user-one", "agent-two")["id"]
        self.assertEqual(self.service.get_chat("user-one", "agent-two", other)["model"]["id"], "vendor/alpha")
        self.assertIsNone(self.sources[("user-one", "agent-two")].model_selection(other))

    def test_explicit_chat_selection_overrides_user_default_and_can_be_cleared(self) -> None:
        self.service.set_chat_model("user-one", "agent-one", self.chat, "vendor/beta")
        self.service.update_preferences("user-one", UserPreferences(default_llm_model="vendor/alpha"))
        chat = self.service.get_chat("user-one", "agent-one", self.chat)
        self.assertEqual(chat["model"]["id"], "vendor/beta")
        self.assertEqual(chat["model_override"]["id"], "vendor/beta")
        self.service.set_chat_model("user-one", "agent-one", self.chat, None)
        chat = self.service.get_chat("user-one", "agent-one", self.chat)
        self.assertEqual(chat["model"]["id"], "vendor/alpha")
        self.assertIsNone(chat["model_override"])
        self.assertEqual(chat["messages"], [])

    def test_cached_agent_refreshes_default_without_rebuilding_embeddings(self) -> None:
        list(self.service.send("user-one", "agent-one", self.chat, "First"))
        agent = self.agents[("user-one", "agent-one")]
        index = agent.index
        self.service.update_preferences("user-one", UserPreferences(default_llm_model="vendor/alpha"))
        list(self.service.send("user-one", "agent-one", self.chat, "Second"))
        self.assertIs(self.agents[("user-one", "agent-one")], agent)
        self.assertIs(agent.index, index)
        self.assertEqual(self.used_models[("user-one", "agent-one")][-1], "vendor/alpha")

    def test_changing_user_default_does_not_change_an_inflight_generation(self) -> None:
        started, release = self._gate("Original")
        with ThreadPoolExecutor(max_workers=1) as pool:
            running = pool.submit(
                lambda: list(self.service.send("user-one", "agent-one", self.chat, "Original")),
            )
            self.assertTrue(started.wait(2))
            self.service.update_preferences("user-one", UserPreferences(default_llm_model="vendor/alpha"))
            self.assertEqual(self.agents[("user-one", "agent-one")].config.models.chat, "vendor/default")
            release.set()
            running.result(timeout=3)
        list(self.service.send("user-one", "agent-one", self.chat, "Following"))
        self.assertEqual(self.used_models[("user-one", "agent-one")], ["vendor/default", "vendor/alpha"])

    def test_explicit_override_is_preserved_when_runtime_defaults_change(self) -> None:
        self.service.set_chat_model("user-one", "agent-one", self.chat, "vendor/beta")
        list(self.service.send("user-one", "agent-one", self.chat, "First"))
        self.service.update_preferences("user-one", UserPreferences(default_llm_model="vendor/alpha"))
        list(self.service.send("user-one", "agent-one", self.chat, "Following"))
        self.assertEqual(self.used_models[("user-one", "agent-one")], ["vendor/beta", "vendor/beta"])

    def test_invalid_or_unavailable_default_is_not_saved(self) -> None:
        with self.assertRaises(ValueError):
            self.service.update_preferences("user-one", UserPreferences(default_llm_model="vendor/missing"))
        self.catalog.side_effect = CatalogUnavailable("Offline")
        with self.assertRaises(CatalogUnavailable):
            self.service.update_preferences("user-one", UserPreferences(default_llm_model="vendor/alpha"))
        self.assertEqual(self.service.get_preferences("user-one")["default_llm_model"], "vendor/default")

    def test_stt_model_is_a_user_setting_shared_by_all_chats(self) -> None:
        with patch.object(self.service, "model_catalog", return_value={"models": [{"id": "stt/default"}]}):
            self.service.update_preferences("user-one", UserPreferences(default_stt_model="stt/default"))
        self.assertEqual(self.service.get_chat("user-one", "agent-one", self.chat)["stt_model"], "stt/default")
        other = self.service.create_chat("user-one", "agent-two")["id"]
        self.assertEqual(self.service.get_chat("user-one", "agent-two", other)["stt_model"], "stt/default")
        self.assertFalse(hasattr(self.service, "set_chat_transcription_model"))

    def test_transcription_uses_user_default_stt_model(self) -> None:
        with patch.object(self.service, "model_catalog", return_value={"models": [{"id": "stt/chat"}]}):
            self.service.update_preferences("user-one", UserPreferences(default_stt_model="stt/chat"))
        resolved = Mock(provider_name="openrouter", api_key="test")
        transcriber = Mock()
        transcriber.transcribe.return_value = "Transcript"
        attachment = {
            "modality": "audio", "name": "audio", "data_url": "data:audio/wav;base64,YQ==", "format": "wav",
        }
        with patch("src.service.chat.load_agent", return_value=resolved):
            with patch("src.service.chat.OpenRouterProvider", return_value=transcriber):
                self.service._transcribe_audio("user-one", "agent-one", self.chat, "", [attachment])
        self.assertEqual(transcriber.transcribe.call_args.kwargs["model"], "stt/chat")

    def test_invalid_audio_chat_returns_not_found_before_provider_access(self) -> None:
        attachment = {
            "modality": "audio", "name": "fixture.wav", "mime_type": "audio/wav", "format": "wav",
            "data_url": "data:audio/wav;base64,YWFhYWFhYWFhYWFh",
        }
        with patch("src.service.chat.load_agent") as load, patch("src.service.chat.OpenRouterProvider") as provider:
            with TestClient(create_app(self.service), raise_server_exceptions=False) as client:
                self.assertEqual(client.post(
                    "/api/login", json={"user_id": "user-one", "password": "test-password-123"},
                ).status_code, 200)
                for chat_id in ("nonexistent", "..%2Fescape"):
                    with self.subTest(chat_id=chat_id):
                        response = client.post(
                            f"/api/agents/agent-one/chats/{chat_id}/messages",
                            json={"attachments": [attachment]},
                        )
                        self.assertEqual(response.status_code, 404)
                for chat_id in ("nonexistent", "../escape", "..\\escape"):
                    with self.subTest(chat_id=chat_id), self.assertRaises(UnknownChat):
                        self.service.send("user-one", "agent-one", chat_id, "", attachments=[attachment])
                load.assert_not_called()
                provider.assert_not_called()

    def test_chat_deleted_during_transcription_is_revalidated_before_enqueue(self) -> None:
        attachment = {
            "modality": "audio", "name": "fixture.wav", "mime_type": "audio/wav", "format": "wav",
            "data_url": "data:audio/wav;base64,YWFhYWFhYWFhYWFh",
        }
        source = self.sources[("user-one", "agent-one")]
        transcriber = Mock()

        def transcribe(*args, **kwargs) -> str:
            source.delete_chat(self.chat)
            return "Transcript"

        transcriber.transcribe.side_effect = transcribe
        with patch("src.service.chat.load_agent", return_value=Mock(provider_name="openrouter", api_key="fixture")):
            with patch("src.service.chat.OpenRouterProvider", return_value=transcriber):
                with self.assertRaises(UnknownChat):
                    self.service.send("user-one", "agent-one", self.chat, "", attachments=[attachment])
        self.assertFalse(self.agents)

    def test_deleting_chat_removes_its_sidecars_including_legacy_stt_overrides(self) -> None:
        source = self.sources[("user-one", "agent-one")]
        source.set_model(self.chat, "vendor/alpha")
        legacy = source.root / f"{self.chat}.stt.json"
        legacy.write_text('{"id": "stt/chat"}', encoding="utf-8")
        source.delete_chat(self.chat)
        self.assertFalse((source.root / f"{self.chat}.model.json").exists())
        self.assertFalse(legacy.exists())


class AgentModelSchemaTests(unittest.TestCase):
    def test_agent_configuration_no_longer_accepts_models(self) -> None:
        with self.assertRaises(ValueError):
            AgentConfig(id="aria", name="Aria", models=ModelsConfig())

if __name__ == "__main__":
    unittest.main()
