from __future__ import annotations

import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

from src.config.favorites import FavoritesError, ModelFavorites
from src.config.models import ProviderConfig, UserConfig
from src.providers.catalog import CatalogUnavailable, OpenRouterModel
from src.service.chat import ChatService
from src.web.app import create_app
from tests.support.auth_helpers import TEST_PASSWORD_HASH, login
from tests.support.provider_helpers import isolate_providers


class ModelFavoritesStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        for user in ("user-one", "user-two"):
            (self.root / user).mkdir()
        self.store = ModelFavorites(self.root)
        self.path = self.root / "user-one" / "openrouter-favorites.json"

    def test_new_user_has_no_favorites_without_creating_a_file(self) -> None:
        self.assertEqual(self.store.list("user-one"), [])
        self.assertFalse(self.path.exists())

    def test_favorites_survive_new_store_instances_and_are_isolated(self) -> None:
        self.store.update("user-one", "vendor/model:free", favorite=True)
        self.store.update("user-two", "vendor/other", favorite=True)
        reopened = ModelFavorites(self.root)
        self.assertEqual(reopened.list("user-one"), ["vendor/model:free"])
        self.assertEqual(reopened.list("user-two"), ["vendor/other"])

    def test_add_and_remove_are_idempotent_and_preserve_order(self) -> None:
        for model in ("vendor/b", "vendor/a", "vendor/b"):
            self.store.update("user-one", model, favorite=True)
        self.assertEqual(self.store.list("user-one"), ["vendor/b", "vendor/a"])
        for model in ("vendor/b", "vendor/b", "vendor/missing"):
            self.assertEqual(self.store.update("user-one", model, favorite=False), ["vendor/a"])

    def test_invalid_user_paths_never_escape_configuration(self) -> None:
        for user in ("../user-one", "..\\user-one", "user-one/../user-two", "", "a" * 65):
            with self.subTest(user=user), self.assertRaises(ValueError):
                self.store.update(user, "vendor/model", favorite=True)
        with self.assertRaises(ValueError):
            self.store.list("not-configured")
        self.assertFalse(self.path.exists())

    def test_invalid_file_is_not_overwritten_with_defaults(self) -> None:
        for content in ('{bad json', '{}', '{"model_ids": [123]}', '{"model_ids": [""]}', '{"unexpected": []}'):
            with self.subTest(content=content):
                self.path.write_text(content, encoding="utf-8")
                with self.assertRaises(FavoritesError):
                    self.store.list("user-one")
                with self.assertRaises(FavoritesError):
                    self.store.update("user-one", "vendor/model", favorite=True)
                self.assertEqual(self.path.read_text(encoding="utf-8"), content)

    def test_resolved_preferences_cannot_target_another_users_directory(self) -> None:
        other = self.root / "user-two" / "openrouter-favorites.json"
        with patch("src.config.favorites.Path.resolve", return_value=other):
            with self.assertRaises(ValueError):
                self.store.list("user-one")

    def test_atomic_replace_failure_preserves_existing_file_and_cleans_temporary(self) -> None:
        self.store.update("user-one", "vendor/original", favorite=True)
        with patch("src.config.favorites.Path.replace", side_effect=PermissionError("denied")):
            with self.assertRaisesRegex(FavoritesError, "permissions"):
                self.store.update("user-one", "vendor/new", favorite=True)
        self.assertEqual(self.store.list("user-one"), ["vendor/original"])
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_concurrent_updates_do_not_lose_favorites(self) -> None:
        ids = [f"vendor/model-{number}" for number in range(20)]
        with ThreadPoolExecutor(max_workers=4) as pool:
            list(pool.map(lambda model: self.store.update("user-one", model, favorite=True), ids))
        self.assertCountEqual(self.store.list("user-one"), ids)
        self.assertEqual(len(self.store.list("user-one")), len(ids))

    def test_user_yaml_is_never_rewritten(self) -> None:
        config = self.path.parent / "user.yaml"
        original = "# Keep my comments\nid: user-one\n"
        config.write_text(original, encoding="utf-8")
        self.store.update("user-one", "vendor/model", favorite=True)
        self.assertEqual(config.read_text(encoding="utf-8"), original)


class ModelFavoritesWebTests(unittest.TestCase):
    def setUp(self) -> None:
        isolate_providers(self)
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.users = {}
        for user in ("user-one", "user-two"):
            (self.root / user).mkdir()
            self.users[user] = UserConfig(max_level="admin", 
                id=user, name=user, provider=ProviderConfig(name="openrouter"),
                password_hash=TEST_PASSWORD_HASH, agents=[],
            )
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
        self.service = ChatService(agent_factory=Mock())
        self.client = TestClient(create_app(self.service))
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        login(self.client, "user-one")
        self.url = "/api/models/favorites"
        catalog_patch = patch("src.service.providers.list_openrouter_models", return_value=[
            OpenRouterModel(
                id="vendor/model:free", name="Model", description="Test", zdr_providers=["Private"],
            ),
        ])
        self.catalog = catalog_patch.start()
        self.addCleanup(catalog_patch.stop)

    def add(self, model: str = "vendor/model:free"):
        return self.client.post(self.url, json={"model_id": model})

    def test_save_reload_and_remove_model_id_with_slash_and_variant(self) -> None:
        self.assertEqual(self.client.get(self.url).json(), [])
        response = self.add()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), ["vendor/model:free"])
        restarted = ChatService(agent_factory=Mock())
        self.assertEqual(restarted.list_favorite_models("user-one"), ["vendor/model:free"])
        self.assertEqual(self.add().json(), ["vendor/model:free"])
        response = self.client.delete(self.url + "/vendor/model:free")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), [])

    def test_favorites_are_scoped_to_cookie_user(self) -> None:
        self.add()
        login(self.client, "user-two")
        self.assertEqual(self.client.get(self.url).json(), [])
        self.client.delete(self.url + "/vendor/model:free")
        login(self.client, "user-one")
        self.assertEqual(self.client.get(self.url).json(), ["vendor/model:free"])

    def test_non_zdr_or_inexact_model_id_cannot_be_added(self) -> None:
        for require_zdr in (True, False):
            self.users["user-one"].require_zdr = require_zdr
            for model in ("vendor/retired", "vendor/model", "VENDOR/MODEL:FREE"):
                with self.subTest(model=model, require_zdr=require_zdr):
                    response = self.add(model)
                    self.assertEqual(response.status_code, 400)
                    # the ZDR filter is only mentioned while it applies
                    self.assertEqual("ZDR" in response.json()["detail"], require_zdr)
        self.assertEqual(self.client.get(self.url).json(), [])

    def test_the_catalog_check_follows_the_user_policy_unless_the_request_overrides_it(self) -> None:
        self.add()
        self.catalog.assert_called_with("vendor/model:free", zdr=False)
        self.users["user-one"].require_zdr = True
        self.add()
        self.catalog.assert_called_with("vendor/model:free", zdr=True)
        self.client.post(self.url, json={"model_id": "vendor/model:free", "zdr": False})
        self.catalog.assert_called_with("vendor/model:free", zdr=False)

    def test_catalog_failure_does_not_change_favorites(self) -> None:
        self.add()
        self.catalog.side_effect = CatalogUnavailable("Catalog unavailable")
        with self.assertLogs("recall.web", level="WARNING"):
            self.assertEqual(self.add("vendor/other").status_code, 502)
        self.assertEqual(self.client.get(self.url).json(), ["vendor/model:free"])

    def test_list_and_remove_retired_model_do_not_need_catalog_or_openrouter_provider(self) -> None:
        self.add()
        self.catalog.reset_mock()
        self.catalog.side_effect = CatalogUnavailable("Offline")
        self.users["user-one"].provider.name = "ollama"
        self.assertEqual(self.client.get(self.url).json(), ["vendor/model:free"])
        self.assertEqual(self.client.delete(self.url + "/vendor/model:free").json(), [])
        self.catalog.assert_not_called()
        self.assertEqual(self.add().status_code, 400)

    def test_corrupt_storage_is_reported_without_discarding_it(self) -> None:
        path = self.root / "user-one" / "openrouter-favorites.json"
        path.write_text("invalid", encoding="utf-8")
        with self.assertLogs("recall.web", level="ERROR"):
            response = self.client.get(self.url)
        self.assertEqual(response.status_code, 500)
        self.assertIn("Could not read model favorites", response.json()["detail"])
        self.assertEqual(path.read_text(encoding="utf-8"), "invalid")

    def test_unauthenticated_or_unknown_user_cannot_mutate_favorites(self) -> None:
        for user in (None, "missing", "..\\user-two"):
            with self.subTest(user=user):
                self.client.cookies.clear()
                if user is not None:
                    self.client.cookies.set("recall_user", user)
                self.assertEqual(self.client.get(self.url).status_code, 401)
                self.assertEqual(self.add().status_code, 401)
                self.assertEqual(self.client.delete(self.url + "/vendor/model:free").status_code, 401)
                if user is not None:
                    response = self.client.post(
                        "/api/login", json={"user_id": user, "password": "test-password-123"},
                    )
                    self.assertEqual(response.status_code, 401)
        self.catalog.assert_not_called()
        self.assertFalse((self.root / "user-one" / "openrouter-favorites.json").exists())

    def test_empty_model_id_is_rejected(self) -> None:
        self.assertEqual(self.add("").status_code, 422)
        self.catalog.assert_not_called()


if __name__ == "__main__":
    unittest.main()
