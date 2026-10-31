from __future__ import annotations

import multiprocessing
import os
import unittest
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from src.config.models import AgentConfig, ProviderConfig, UserConfig
from src.config.preferences import PreferencesError, UserPreferences, UserPreferencesStore
from src.service.chat import ChatService
from src.web.app import create_app


def _update_in_process(root: Path, field: str, value: str | bool) -> None:
    store = UserPreferencesStore(root)
    for _ in range(10):
        store.update("user-one", UserPreferences.model_validate({field: value}))


class UserPreferencesStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for user in ("user-one", "user-two"):
            (self.root / user).mkdir()
        self.store = UserPreferencesStore(self.root)
        self.path = self.root / "user-one" / "preferences.json"

    def test_missing_preferences_do_not_create_files(self) -> None:
        prefs = self.store.get("user-one")
        self.assertEqual(prefs.model_dump(), {
            "show_thinking": True, "language": "en", "agent_id": None,
        })
        self.assertEqual(prefs.model_fields_set, set())
        self.assertEqual(list(self.path.parent.iterdir()), [])

    def test_partial_updates_are_persistent_isolated_and_not_cached(self) -> None:
        other_process = UserPreferencesStore(self.root)
        self.store.update("user-one", UserPreferences(language="es"))
        other_process.update("user-one", UserPreferences(show_thinking=False))
        self.assertEqual(self.store.get("user-one").model_dump(exclude_unset=True), {
            "language": "es", "show_thinking": False,
        })
        self.assertEqual(other_process.get("user-one"), self.store.get("user-one"))
        self.assertEqual(self.store.get("user-two").model_fields_set, set())

    def test_invalid_user_paths_never_escape_configuration(self) -> None:
        for user in ("../user-one", "..\\user-one", "user-one/../user-two", "", "a" * 65, "missing"):
            with self.subTest(user=user), self.assertRaises(ValueError):
                self.store.update(user, UserPreferences(language="es"))
        self.assertFalse(self.path.exists())

    def test_resolved_paths_cannot_target_another_users_directory(self) -> None:
        other = self.root / "user-two" / "preferences.json"
        with patch("src.config.preferences.Path.resolve", return_value=other):
            with self.assertRaises(ValueError):
                self.store.get("user-one")

    def test_corrupt_storage_is_not_replaced_with_defaults(self) -> None:
        for content in (
            "invalid", "[]", '{"show_thinking": "false"}', '{"language": "fr"}',
            '{"language": null}', '{"show_thinking": null}', '{"agent_id": "../other"}',
            '{"agent_id": 123}', '{"unknown": true}',
        ):
            with self.subTest(content=content):
                self.path.write_text(content, encoding="utf-8")
                with self.assertRaises(PreferencesError):
                    self.store.get("user-one")
                with self.assertRaises(PreferencesError):
                    self.store.update("user-one", UserPreferences(language="en"))
                self.assertEqual(self.path.read_text(encoding="utf-8"), content)

    def test_failed_replace_preserves_previous_file_and_cleans_temporary(self) -> None:
        self.store.update("user-one", UserPreferences(language="es"))
        with patch("src.utils.files.Path.replace", side_effect=PermissionError("denied")):
            with self.assertRaisesRegex(PreferencesError, "permissions"):
                self.store.update("user-one", UserPreferences(show_thinking=False))
        self.assertEqual(self.store.get("user-one").model_dump(exclude_unset=True), {"language": "es"})
        self.assertEqual(sorted(path.name for path in self.path.parent.iterdir()), [
            "preferences.json", "preferences.lock",
        ])

    @unittest.skipUnless(os.name == "nt", "Windows replacement contention")
    def test_transient_windows_replace_error_is_retried_without_losing_changes(self) -> None:
        original_replace = Path.replace
        denied = PermissionError(13, "Access denied", str(self.path), 5)
        calls = 0

        def replace(path: Path, target: Path) -> Path:
            nonlocal calls
            calls += 1
            if calls == 1:
                raise denied
            return original_replace(path, target)

        with patch("src.utils.files.Path.replace", new=replace):
            self.store.update("user-one", UserPreferences(language="es"))
        self.assertEqual(calls, 2)
        self.assertEqual(self.store.get("user-one").language, "es")
        self.assertFalse(list(self.path.parent.glob("*.tmp")))

    @unittest.skipUnless(os.name == "nt", "Windows replacement contention")
    def test_persistent_windows_replace_error_is_bounded_and_reported(self) -> None:
        self.store.update("user-one", UserPreferences(language="es"))
        denied = PermissionError(13, "Access denied", str(self.path), 5)
        with patch("src.utils.files.Path.replace", side_effect=denied) as replace:
            with patch("src.utils.files.sleep") as sleep:
                with self.assertRaises(PreferencesError):
                    self.store.update("user-one", UserPreferences(language="en"))
        self.assertEqual(replace.call_count, 5)
        self.assertEqual(sleep.call_count, 4)
        self.assertEqual(self.store.get("user-one").language, "es")
        self.assertFalse(list(self.path.parent.glob("*.tmp")))

    def test_failed_lock_reports_actionable_error_without_mutating_preferences(self) -> None:
        with patch("src.config.preferences.exclusive_file_lock", side_effect=PermissionError("denied")):
            with self.assertRaisesRegex(PreferencesError, "lock"):
                self.store.update("user-one", UserPreferences(language="es"))
        self.assertFalse(self.path.exists())

    def test_independent_store_instances_serialize_thread_updates(self) -> None:
        def update(field: str) -> None:
            store = UserPreferencesStore(self.root)
            store.update("user-one", UserPreferences.model_validate({
                field: {"language": "es", "show_thinking": False, "agent_id": "agent-b"}[field],
            }))

        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(update, ("language", "show_thinking", "agent_id")))
        self.assertEqual(self.store.get("user-one").model_dump(), {
            "show_thinking": False, "language": "es", "agent_id": "agent-b",
        })

    def test_concurrent_process_updates_preserve_other_fields(self) -> None:
        with ProcessPoolExecutor(max_workers=3, mp_context=multiprocessing.get_context("spawn")) as pool:
            tasks = [
                pool.submit(_update_in_process, self.root, field, value)
                for field, value in (("language", "es"), ("show_thinking", False), ("agent_id", "agent-b"))
            ]
            for task in tasks:
                task.result(timeout=60)
        self.assertEqual(self.store.get("user-one").model_dump(), {
            "show_thinking": False, "language": "es", "agent_id": "agent-b",
        })

    def test_user_yaml_and_favorites_are_not_rewritten(self) -> None:
        files = {
            "user.yaml": "# Preserve comments\nid: user-one\n",
            "openrouter-favorites.json": '{"model_ids":["vendor/model"]}\n',
        }
        for name, content in files.items():
            (self.path.parent / name).write_text(content, encoding="utf-8")
        self.store.update("user-one", UserPreferences(language="es"))
        for name, content in files.items():
            self.assertEqual((self.path.parent / name).read_text(encoding="utf-8"), content)


class UserPreferencesWebTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.users = {
            user: UserConfig(
                id=user, name=user, language=language, provider=ProviderConfig(name="openrouter"),
                agents=agents,
            )
            for user, language, agents in (
                ("user-one", "es-ES", ["agent-a", "agent-b"]),
                ("user-two", "en", ["agent-c"]),
                ("no-agents", "en", []),
            )
        }
        for user in self.users:
            (self.root / user).mkdir()
        root_patch = patch("src.service.chat.CONFIG_DIR", self.root)
        root_patch.start()
        self.addCleanup(root_patch.stop)

        def load_user(user_id: str) -> UserConfig:
            if user_id not in self.users:
                raise FileNotFoundError(user_id)
            return self.users[user_id]

        user_patch = patch("src.service.chat.load_user", side_effect=load_user)
        user_patch.start()
        self.addCleanup(user_patch.stop)
        agent_patch = patch(
            "src.service.chat.load_agent_config",
            side_effect=lambda user, agent: AgentConfig(id=agent, name=agent, personality_prompt="Test"),
        )
        self.load_agent_config = agent_patch.start()
        self.addCleanup(agent_patch.stop)
        self.factory = Mock()
        self.service = ChatService(agent_factory=self.factory)
        self.client = TestClient(create_app(self.service))
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        self.client.cookies.set("recall_user", "user-one")
        self.url = "/api/preferences"
        self.path = self.root / "user-one" / "preferences.json"

    def test_defaults_use_yaml_language_and_first_owned_agent(self) -> None:
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {
            "show_thinking": True, "language": "es", "agent_id": "agent-a",
        })
        self.assertFalse(self.path.exists())
        self.factory.assert_not_called()

    def test_updates_are_visible_to_another_service_and_survive_restart(self) -> None:
        telegram = ChatService(agent_factory=Mock())
        self.assertEqual(telegram.get_preferences("user-one")["language"], "es")
        response = self.client.patch(self.url, json={"language": "en", "agent_id": "agent-b"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(telegram.get_preferences("user-one"), response.json())
        telegram.update_preferences("user-one", UserPreferences(show_thinking=False))
        self.assertEqual(self.client.get(self.url).json(), {
            "show_thinking": False, "language": "en", "agent_id": "agent-b",
        })
        self.assertEqual(ChatService().get_preferences("user-one"), self.client.get(self.url).json())

    def test_unset_fields_still_follow_yaml_and_null_resets_agent_selection(self) -> None:
        self.client.patch(self.url, json={"show_thinking": False})
        self.users["user-one"].language = "en-US"
        self.users["user-one"].agents.reverse()
        self.assertEqual(self.client.get(self.url).json(), {
            "show_thinking": False, "language": "en", "agent_id": "agent-b",
        })
        self.client.patch(self.url, json={"agent_id": "agent-a"})
        response = self.client.patch(self.url, json={"agent_id": None})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["agent_id"], "agent-b")

    def test_preferences_are_scoped_to_cookie_user(self) -> None:
        self.client.patch(self.url, json={"language": "en", "show_thinking": False, "agent_id": "agent-b"})
        self.client.cookies.set("recall_user", "user-two")
        self.assertEqual(self.client.get(self.url).json(), {
            "show_thinking": True, "language": "en", "agent_id": "agent-c",
        })
        self.client.patch(self.url, json={"language": "es"})
        self.client.cookies.set("recall_user", "user-one")
        self.assertEqual(self.client.get(self.url).json(), {
            "show_thinking": False, "language": "en", "agent_id": "agent-b",
        })

    def test_foreign_or_missing_agent_is_rejected_before_preferences_are_changed(self) -> None:
        self.client.patch(self.url, json={"language": "en"})
        for agent in ("agent-c", "missing"):
            with self.subTest(agent=agent):
                response = self.client.patch(self.url, json={"agent_id": agent, "show_thinking": False})
                self.assertEqual(response.status_code, 404)
                self.assertEqual(self.service.get_preferences("user-one"), {
                    "show_thinking": True, "language": "en", "agent_id": "agent-a",
                })
        self.load_agent_config.side_effect = FileNotFoundError("missing config")
        response = self.client.patch(self.url, json={"agent_id": "agent-b", "show_thinking": False})
        self.assertEqual(response.status_code, 404)
        self.assertEqual(UserPreferencesStore(self.root).get("user-one").model_dump(exclude_unset=True), {
            "language": "en",
        })

    def test_removed_saved_agent_is_an_explicit_error_and_can_be_reselected(self) -> None:
        self.client.patch(self.url, json={"agent_id": "agent-b"})
        self.users["user-one"].agents.remove("agent-b")
        self.assertEqual(self.client.get(self.url).status_code, 404)
        original = self.path.read_text(encoding="utf-8")
        self.assertEqual(self.client.patch(self.url, json={"language": "en"}).status_code, 404)
        self.assertEqual(self.path.read_text(encoding="utf-8"), original)
        response = self.client.patch(self.url, json={"agent_id": "agent-a"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["agent_id"], "agent-a")

    def test_no_agents_is_represented_explicitly_without_building_an_agent(self) -> None:
        self.client.cookies.set("recall_user", "no-agents")
        self.assertEqual(self.client.get(self.url).json()["agent_id"], None)
        self.assertEqual(self.client.patch(self.url, json={"language": "es"}).status_code, 200)
        self.factory.assert_not_called()

    def test_invalid_fields_and_coercions_are_rejected(self) -> None:
        for body in (
            {"show_thinking": "false"}, {"show_thinking": 0}, {"show_thinking": None},
            {"language": "fr"}, {"language": "es-ES"}, {"language": None},
            {"agent_id": "../agent-a"}, {"agent_id": "..\\agent-a"}, {"agent_id": ""},
            {"agent_id": 123}, {"user_id": "user-two"}, {"unexpected": True},
        ):
            with self.subTest(body=body):
                self.assertEqual(self.client.patch(self.url, json=body).status_code, 422)
        self.assertFalse(self.path.exists())

    def test_unauthenticated_or_unknown_users_cannot_read_or_write_preferences(self) -> None:
        for user in (None, "missing", "..\\user-two"):
            with self.subTest(user=user):
                self.client.cookies.clear()
                if user is not None:
                    self.client.cookies.set("recall_user", user)
                self.assertEqual(self.client.get(self.url).status_code, 401)
                self.assertEqual(self.client.patch(self.url, json={"language": "es"}).status_code, 401)
        self.assertFalse(self.path.exists())

    def test_corrupt_preferences_produce_logged_http_errors_without_replacing_file(self) -> None:
        self.path.write_text("invalid", encoding="utf-8")
        with self.assertLogs("recall.web", level="ERROR"):
            self.assertEqual(self.client.get(self.url).status_code, 500)
            self.assertEqual(self.client.patch(self.url, json={"language": "es"}).status_code, 500)
        self.assertEqual(self.path.read_text(encoding="utf-8"), "invalid")

    def test_write_failures_are_reported_without_claiming_success(self) -> None:
        with patch("src.utils.files.Path.replace", side_effect=PermissionError("denied")):
            with self.assertLogs("recall.web", level="ERROR"):
                response = self.client.patch(self.url, json={"language": "en"})
        self.assertEqual(response.status_code, 500)
        self.assertIn("permisos", response.json()["detail"])
        self.assertFalse(self.path.exists())

    def test_settings_do_not_touch_agent_execution_or_chat_state(self) -> None:
        slot = self.service._slot("user-one", "agent-a")
        slot.agent = Mock()
        slot.agent.chat_id = "existing-chat"
        response = self.client.patch(self.url, json={
            "agent_id": "agent-b", "show_thinking": False, "language": "en",
        })
        self.assertEqual(response.status_code, 200)
        self.assertEqual(slot.agent.chat_id, "existing-chat")
        slot.agent.assert_not_called()
        self.factory.assert_not_called()


if __name__ == "__main__":
    unittest.main()
