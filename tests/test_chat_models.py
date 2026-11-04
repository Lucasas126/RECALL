from __future__ import annotations

import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from fastapi.testclient import TestClient

import test_service_steering as steering
from src.agent.agent import Agent
from src.config.favorites import ModelFavorites
from src.config.models import AgentConfig, ModelsConfig, ResolvedAgent, UserConfig
from src.core.sources.conversations import ChatMetadataError, ConversationsSource
from src.providers.catalog import CatalogUnavailable, ModelPricing, OpenRouterModel
from src.service.chat import AgentBusy, ChatService, UnknownAgent, UnknownChat
from src.utils.i18n import InputError
from src.web.app import create_app
from auth_helpers import login
from provider_helpers import CLAUDE_LOGGED_IN, isolate_providers


class ConversationModelTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = ConversationsSource(self.root)
        self.chat = self.source.create()

    def test_selection_is_persistent_without_creating_history_or_chats(self) -> None:
        self.assertIsNone(self.source.model_selection(self.chat))
        self.source.set_model(self.chat, "vendor/model")
        reopened = ConversationsSource(self.root)
        self.assertEqual(reopened.model_selection(self.chat).id, "vendor/model")
        self.assertEqual(reopened.load(self.chat), [])
        self.assertEqual(reopened.chats(), [self.chat])
        self.assertEqual(reopened.summary(self.chat)["turns"], 0)

    def test_deleting_or_pruning_chat_removes_selection_sidecar(self) -> None:
        self.source.set_model(self.chat, "vendor/model")
        self.source.delete_chat(self.chat)
        self.assertEqual(list(self.root.iterdir()), [])
        chat = self.source.create()
        self.source.set_model(chat, "vendor/model")
        self.assertEqual(self.source.prune_empty(), [chat])
        self.assertEqual(list(self.root.iterdir()), [])

    def test_bad_metadata_is_explicit_and_never_treated_as_default(self) -> None:
        path = self.root / f"{self.chat}.model.json"
        path.write_text('{"provider": "unknown", "id": "vendor/model"}', encoding="utf-8")
        with self.assertRaises(ChatMetadataError):
            self.source.model_selection(self.chat)

    def test_unknown_chat_and_traversal_are_rejected(self) -> None:
        with self.assertRaises(FileNotFoundError):
            self.source.set_model("missing", "vendor/model")
        for chat in ("../escape", "..\\escape"):
            with self.subTest(chat=chat), self.assertRaises(ValueError):
                self.source.model_selection(chat)

    def test_failed_write_preserves_previous_selection(self) -> None:
        self.source.set_model(self.chat, "vendor/original")
        with patch("src.utils.files.Path.replace", side_effect=PermissionError("denied")):
            with self.assertRaises(ChatMetadataError):
                self.source.set_model(self.chat, "vendor/new")
        self.assertEqual(self.source.model_selection(self.chat).id, "vendor/original")
        self.assertFalse(list(self.root.glob("*.tmp")))

    def test_selection_without_a_flag_follows_the_user_and_a_stored_flag_persists(self) -> None:
        path = self.root / f"{self.chat}.model.json"
        path.write_text('{"provider":"openrouter","id":"vendor/model"}', encoding="utf-8")
        self.assertIsNone(self.source.model_selection(self.chat).zdr)
        self.source.set_model(self.chat, "vendor/model", zdr=False)
        self.assertIs(ConversationsSource(self.root).model_selection(self.chat).zdr, False)
        self.source.set_model(self.chat, "vendor/model", zdr=True)
        self.assertIs(ConversationsSource(self.root).model_selection(self.chat).zdr, True)
        self.source.set_model(self.chat, "vendor/model")
        self.assertNotIn("zdr", path.read_text(encoding="utf-8"))

    def test_a_claude_subscription_chat_cannot_store_zdr(self) -> None:
        self.source.set_model(self.chat, "claude_subscription:sonnet", zdr=False)
        with self.assertRaises(ValueError):
            self.source.set_model(self.chat, "claude_subscription:sonnet", zdr=True)

    def test_reasoning_effort_sidecar_is_persistent_removed_with_chat_and_validated(self) -> None:
        self.source.set_reasoning_effort(self.chat, "max")
        self.assertEqual(ConversationsSource(self.root).reasoning_effort_selection(self.chat).effort, "max")
        self.source.set_reasoning_effort(self.chat, None)
        self.assertIsNone(self.source.reasoning_effort_selection(self.chat))
        self.source.set_reasoning_effort(self.chat, "low")
        self.source.delete_chat(self.chat)
        self.assertEqual(list(self.root.iterdir()), [])
        chat = self.source.create()
        (self.root / f"{chat}.effort.json").write_text('{"effort": "ultra"}', encoding="utf-8")
        with self.assertRaises(ChatMetadataError):
            self.source.reasoning_effort_selection(chat)

    def test_invalid_stored_policy_is_an_error_not_an_opt_out(self) -> None:
        path = self.root / f"{self.chat}.model.json"
        path.write_text('{"provider":"openrouter","id":"vendor/model","zdr":"false"}', encoding="utf-8")
        with self.assertRaises(ChatMetadataError):
            self.source.model_selection(self.chat)


class ModelFixture(steering.SteeringFixture):
    require_zdr = True  # the user's policy; the default-off mode is covered by the ...WithoutZdrTests classes

    def setUp(self) -> None:
        self.used_models: dict[tuple[str, str], list[str]] = {}
        self.used_policies: dict[tuple[str, str], list[bool]] = {}
        super().setUp()
        isolate_providers(self)
        preferences = self.root / "preferences"
        for user in ("user-one", "user-two"):
            (preferences / user).mkdir(parents=True)
        self.service._favorites = ModelFavorites(preferences)
        agent_patch = patch(
            "src.service.chat.load_agent_config",
            side_effect=lambda user, agent: AgentConfig(
                id=agent, name=agent,
            ),
        )
        agent_patch.start()
        self.addCleanup(agent_patch.stop)
        self.catalog_models = [
            OpenRouterModel(
                id=f"vendor/{name}", name=name.title(), description="Test", zdr_providers=["Private"],
                pricing=ModelPricing(prompt="0.123456789", completion="0"),
            )
            for name in ("default", "alpha", "beta")
        ] + [
            OpenRouterModel(
                id="vendor/retaining", name="Retaining", description="No ZDR routes", zdr_providers=[],
                pricing=ModelPricing(prompt="0.05", completion="0.2"),
            ),
        ]
        catalog_patch = patch(
            "src.service.providers.list_openrouter_models",
            side_effect=lambda query="", *, zdr=False: [
                model for model in self.catalog_models if not zdr or model.zdr_providers
            ],
        )
        self.catalog = catalog_patch.start()
        self.addCleanup(catalog_patch.stop)
        # reasoning-effort support reads the same catalog from the chat service
        support_patch = patch("src.service.chat.list_openrouter_models", new=self.catalog)
        support_patch.start()
        self.addCleanup(support_patch.stop)

    def _user(self, user_id: str) -> UserConfig:
        user = super()._user(user_id)
        user.provider.name = "openrouter"
        user.models.chat = "vendor/default"
        user.require_zdr = self.require_zdr
        return user

    def _factory(self, user: str, agent: str, chat_id: str | None) -> Agent:
        config = ResolvedAgent(
            user_id=user, id=agent, name=agent, require_zdr=self.require_zdr,
            models=ModelsConfig(chat="vendor/default"), provider_name="openrouter", api_key="",
            embeddings_provider_name="openrouter", embeddings_api_key="", reasoning_effort="medium",
            tools=False, associative_memory=self.associative_memory, has_memory=self.memory,
            conversation_messages_retained=self.retained,
        )
        instance = Agent(config, self.providers[(user, agent)], chat_id)
        self.agents[(user, agent)] = instance
        return instance

    def _responder(self, key: tuple[str, str]):
        respond = super()._responder(key)

        def capture(messages, model, **kwargs):
            self.used_models.setdefault(key, []).append(model)
            self.used_policies.setdefault(key, []).append(kwargs["zdr"])
            return respond(messages, model, **kwargs)

        return capture


class ChatModelServiceTests(ModelFixture):
    def send(self, text: str = "Question", chat: str | None = None) -> None:
        list(self.service.send("user-one", "agent-one", chat or self.chat, text))

    def test_default_and_selected_model_are_exposed_without_building_agent(self) -> None:
        self.assertEqual(self.service.get_chat("user-one", "agent-one", self.chat)["model"]["id"], "vendor/default")
        self.service.set_chat_model("user-one", "agent-one", self.chat, "vendor/alpha")
        self.assertEqual(self.service.get_chat("user-one", "agent-one", self.chat)["model"], {
            "provider": "openrouter", "id": "vendor/alpha", "zdr": True, "require_zdr": True,
        })
        self.assertEqual(self.agents, {})

    def test_changing_current_chat_preserves_history_and_resets_model_context_cache(self) -> None:
        self.send("First")
        old_agent = self.agents[("user-one", "agent-one")]
        old_agent._max_context = 10000
        self.service.set_chat_model("user-one", "agent-one", self.chat, "vendor/alpha")
        self.send("Second")
        self.assertEqual(self.used_models[("user-one", "agent-one")], ["vendor/default", "vendor/alpha"])
        self.assertEqual(old_agent.config.models.chat, "vendor/default")
        new_agent = self.agents[("user-one", "agent-one")]
        self.assertIsNot(old_agent, new_agent)
        self.assertTrue(any(message["content"].endswith("First") for message in self.calls[("user-one", "agent-one")][-1]["context"]))
        self.assertEqual(new_agent.provider.context_window.call_args.args[0], "vendor/alpha")

    def test_reopening_chat_and_switching_to_others_does_not_leak_model(self) -> None:
        self.service.set_chat_model("user-one", "agent-one", self.chat, "vendor/alpha")
        self.send("First")
        other = self.service.create_chat("user-one", "agent-one")["id"]
        self.send("Other", other)
        self.send("Back")
        self.assertEqual(self.used_models[("user-one", "agent-one")], [
            "vendor/alpha", "vendor/default", "vendor/alpha",
        ])
        self.assertEqual([
            call.args[0] for call in self.providers[("user-one", "agent-one")].context_window.call_args_list
        ], ["vendor/alpha", "vendor/default"])
        restarted = ChatService(agent_factory=self._factory)
        list(restarted.send("user-one", "agent-one", self.chat, "After restart"))
        self.assertEqual(self.used_models[("user-one", "agent-one")][-1], "vendor/alpha")

    def test_selection_does_not_change_other_agents_or_users(self) -> None:
        self.service.set_chat_model("user-one", "agent-one", self.chat, "vendor/alpha")
        for user, agent in (("user-one", "agent-two"), ("user-two", "agent-one")):
            with self.subTest(user=user, agent=agent):
                chat = self.service.create_chat(user, agent)["id"]
                self.assertEqual(self.service.get_chat(user, agent, chat)["model"]["id"], "vendor/default")
                list(self.service.send(user, agent, chat, "Independent"))
                self.assertEqual(self.used_models[(user, agent)], ["vendor/default"])

    def test_new_model_does_not_reuse_old_token_counts_or_mutate_supplied_config(self) -> None:
        self.send("First")
        original = self.agents[("user-one", "agent-one")].config
        self.service.set_chat_model("user-one", "agent-one", self.chat, "vendor/alpha")
        reopened = Agent(original, self.providers[("user-one", "agent-one")], self.chat)
        self.assertEqual(reopened.config.models.chat, "vendor/alpha")
        self.assertEqual(original.models.chat, "vendor/default")
        self.assertIsNone(reopened._ctx_input_tokens)
        self.assertIsNone(reopened._prev_breakdown)

    def test_favorite_is_not_required_and_cannot_bypass_zdr_validation(self) -> None:
        self.service.set_chat_model("user-one", "agent-one", self.chat, "vendor/alpha")
        self.assertEqual(self.service.list_favorite_models("user-one"), [])
        self.service._favorites.update("user-one", "vendor/retired", favorite=True)
        with self.assertRaisesRegex(ValueError, "ZDR"):
            self.service.set_chat_model("user-one", "agent-one", self.chat, "vendor/retired")
        self.assertEqual(self.sources[("user-one", "agent-one")].model_selection(self.chat).id, "vendor/alpha")

    def test_active_or_queued_agent_cannot_change_model(self) -> None:
        started, release = self._gate("Original")
        original = self.service.send("user-one", "agent-one", self.chat, "Original")
        self.assertTrue(started.wait(2))
        try:
            with self.assertRaises(AgentBusy):
                self.service.set_chat_model("user-one", "agent-one", self.chat, "vendor/alpha")
            other = self.service.create_chat("user-one", "agent-one")["id"]
            self.service.set_chat_model("user-one", "agent-one", other, "vendor/beta")
            queued = self.service.send("user-one", "agent-one", other, "Queued")
            with self.assertRaises(AgentBusy):
                self.service.set_chat_model("user-one", "agent-one", other, "vendor/alpha")
        finally:
            release.set()
        list(original)
        list(queued)
        self.assertIsNone(self.sources[("user-one", "agent-one")].model_selection(self.chat))

    def test_ownership_and_chat_existence_are_checked_before_catalog_access(self) -> None:
        with self.assertRaises(UnknownAgent):
            self.service.set_chat_model("user-one", "not-owned", self.chat, "vendor/alpha")
        with self.assertRaises(UnknownChat):
            self.service.set_chat_model("user-two", "agent-one", self.chat, "vendor/alpha")
        self.catalog.assert_not_called()

    def test_concurrent_model_updates_remain_valid(self) -> None:
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(
                lambda model: self.service.set_chat_model("user-one", "agent-one", self.chat, model),
                ["vendor/alpha", "vendor/beta"],
            ))
        self.assertEqual(len(results), 2)
        self.assertIn(self.sources[("user-one", "agent-one")].model_selection(self.chat).id, {
            "vendor/alpha", "vendor/beta",
        })

    def test_opt_out_is_explicit_persistent_per_chat_and_does_not_leak(self) -> None:
        with self.assertRaises(ValueError):
            self.service.set_chat_model("user-one", "agent-one", self.chat, "vendor/retaining")
        self.service.set_chat_model("user-one", "agent-one", self.chat, "vendor/retaining", zdr=False)
        self.send("Without ZDR")
        other = self.service.create_chat("user-one", "agent-one")["id"]
        self.send("Default policy", other)
        self.send("Back to opt-out")
        self.assertEqual(self.used_policies[("user-one", "agent-one")], [False, True, False])
        restarted = ChatService(agent_factory=self._factory)
        list(restarted.send("user-one", "agent-one", self.chat, "After restart"))
        self.assertFalse(self.used_policies[("user-one", "agent-one")][-1])
        self.service.set_chat_model("user-one", "agent-one", self.chat, "vendor/alpha")
        self.send("ZDR again")
        self.assertTrue(self.used_policies[("user-one", "agent-one")][-1])

    def test_non_zdr_model_can_be_favorited_only_with_explicit_opt_out(self) -> None:
        with self.assertRaises(ValueError):
            self.service.add_favorite_model("user-one", "vendor/retaining")
        self.assertEqual(
            self.service.add_favorite_model("user-one", "vendor/retaining", zdr=False),
            ["vendor/retaining"],
        )

    def test_direct_non_streaming_agent_honors_saved_opt_out(self) -> None:
        self.service.set_chat_model("user-one", "agent-one", self.chat, "vendor/retaining", zdr=False)
        provider = self.providers[("user-one", "agent-one")]
        provider.invoke.side_effect = None
        provider.invoke.return_value = {"response": "Answer", "usage": {"prompt_tokens": 10, "completion_tokens": 5}}
        agent = self._factory("user-one", "agent-one", self.chat)
        self.assertEqual(agent.invoke("Direct sync", stream=False), "Answer")
        self.assertFalse(provider.invoke.call_args.kwargs["zdr"])
        self.assertFalse(self.sources[("user-one", "agent-one")].load(self.chat)[-1]["zdr"])


class ChatModelWebTests(ModelFixture):
    def setUp(self) -> None:
        super().setUp()
        self.client = TestClient(create_app(self.service))
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        login(self.client, "user-one")
        self.url = f"/api/agents/agent-one/chats/{self.chat}/model"

    def test_select_model_and_reopen_chat(self) -> None:
        response = self.client.put(self.url, json={"model_id": "vendor/alpha"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"provider": "openrouter", "id": "vendor/alpha", "zdr": True, "require_zdr": True})
        reopened = self.client.get(self.url.removesuffix("/model"))
        self.assertEqual(reopened.json()["model"], response.json())
        self.assertEqual(self.client.get("/api/me").json()["provider"], "openrouter")

    def test_rename_chat_over_http(self) -> None:
        url = self.url.removesuffix("/model") + "/title"
        response = self.client.put(url, json={"title": "Weekly plan"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["title"], "Weekly plan")
        self.assertEqual(self.client.get(url.removesuffix("/title")).json()["chat"]["title"], "Weekly plan")
        self.assertEqual(self.client.put(url, json={"title": "  "}).status_code, 400)
        missing = "/api/agents/agent-one/chats/missing/title"
        self.assertEqual(self.client.put(missing, json={"title": "Title"}).status_code, 404)

    def test_busy_agent_returns_conflict_without_mutation(self) -> None:
        started, release = self._gate("Original")
        events = self.service.send("user-one", "agent-one", self.chat, "Original")
        self.assertTrue(started.wait(2))
        try:
            self.assertEqual(self.client.put(self.url, json={"model_id": "vendor/alpha"}).status_code, 409)
        finally:
            release.set()
        list(events)
        self.assertIsNone(self.sources[("user-one", "agent-one")].model_selection(self.chat))

    def test_corrupt_metadata_is_an_explicit_server_error(self) -> None:
        source = self.sources[("user-one", "agent-one")]
        (source.root / f"{self.chat}.model.json").write_text("invalid", encoding="utf-8")
        with self.assertLogs("recall.web", level="ERROR"):
            response = self.client.get(self.url.removesuffix("/model"))
        self.assertEqual(response.status_code, 500)

    def test_opt_out_is_accepted_only_as_an_explicit_boolean(self) -> None:
        for value in ("false", 0):
            with self.subTest(value=value):
                response = self.client.put(self.url, json={"model_id": "vendor/retaining", "zdr": value})
                self.assertEqual(response.status_code, 422)
        response = self.client.put(self.url, json={"model_id": "vendor/retaining", "zdr": False})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"provider": "openrouter", "id": "vendor/retaining", "zdr": False, "require_zdr": True})
        self.assertEqual(self.client.get(self.url.removesuffix("/model")).json()["model"], response.json())

    def test_http_catalog_filter_and_favorites_respect_opt_out(self) -> None:
        filtered = self.client.get("/api/models").json()
        unrestricted = self.client.get("/api/models?zdr=false").json()
        self.assertNotIn("vendor/retaining", {model["id"] for model in filtered})
        self.assertIn("vendor/retaining", {model["id"] for model in unrestricted})
        favorite = {"model_id": "vendor/retaining"}
        self.assertEqual(self.client.post("/api/models/favorites", json=favorite).status_code, 400)
        response = self.client.post("/api/models/favorites", json={**favorite, "zdr": False})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), ["vendor/retaining"])

    def test_reasoning_effort_override_is_saved_and_validated_over_http(self) -> None:
        url = self.url.removesuffix("/model") + "/reasoning-effort"
        self.catalog_models[0].parameter_routes = [["tools", "reasoning_effort"]]
        response = self.client.put(url, json={"effort": "high"})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["reasoning_effort"]["override"], "high")
        self.assertEqual(self.client.put(url, json={"effort": "ultra"}).status_code, 422)
        self.assertEqual(self.client.put(url, json={}).status_code, 422)
        self.assertEqual(self.client.put(url, json={"effort": None}).json()["reasoning_effort"]["override"], None)


class ChatModelWithoutZdrTests(ModelFixture):
    """ZDR is not required (the default): nothing is filtered or stored unless a chat opts in."""

    require_zdr = False

    def send(self, text: str = "Question", chat: str | None = None) -> None:
        list(self.service.send("user-one", "agent-one", chat or self.chat, text))

    def sidecar(self, chat: str | None = None) -> Path:
        return self.sources[("user-one", "agent-one")].root / f"{chat or self.chat}.model.json"

    def test_chats_and_catalog_do_not_require_zdr(self) -> None:
        self.assertEqual(self.service.get_chat_models("user-one", "agent-one", self.chat)["model"], {
            "provider": "openrouter", "id": "vendor/default", "zdr": False, "require_zdr": False,
        })
        self.assertIn("vendor/retaining", {model["id"] for model in self.service.list_models("user-one")})
        self.service.set_chat_model("user-one", "agent-one", self.chat, "vendor/retaining")
        self.send("No ZDR")
        self.assertEqual(self.used_policies[("user-one", "agent-one")], [False])
        self.assertNotIn("zdr", self.sidecar().read_text(encoding="utf-8"))

    def test_a_chat_can_opt_in_and_the_catalog_is_then_filtered(self) -> None:
        with self.assertRaises(ValueError):
            self.service.set_chat_model("user-one", "agent-one", self.chat, "vendor/retaining", zdr=True)
        model = self.service.set_chat_model("user-one", "agent-one", self.chat, "vendor/alpha", zdr=True)
        self.assertTrue(model["zdr"])
        self.send("With ZDR")
        other = self.service.create_chat("user-one", "agent-one")["id"]
        self.send("Follows the setting", other)
        self.assertEqual(self.used_policies[("user-one", "agent-one")], [True, False])

    def test_chats_without_a_flag_follow_the_setting_and_a_stored_flag_wins(self) -> None:
        self.send("Follows")
        stored = self.service.create_chat("user-one", "agent-one")["id"]
        self.service.set_chat_model("user-one", "agent-one", stored, "vendor/alpha", zdr=True)
        self.send("Stored", stored)
        self.assertEqual(self.used_policies[("user-one", "agent-one")], [False, True])
        self.require_zdr = True
        restarted = ChatService(agent_factory=self._factory)
        self.assertTrue(restarted.get_chat_models("user-one", "agent-one", self.chat)["model"]["zdr"])
        list(restarted.send("user-one", "agent-one", self.chat, "Now required"))
        self.assertTrue(self.used_policies[("user-one", "agent-one")][-1])
        # a chat that opted out under the old setting keeps its choice
        self.service.set_chat_model("user-one", "agent-one", stored, "vendor/retaining", zdr=False)
        self.assertIs(self.sources[("user-one", "agent-one")].model_selection(stored).zdr, False)

    def test_default_models_are_validated_without_zdr(self) -> None:
        from src.config.preferences import UserPreferences
        saved = self.service.update_preferences("user-one", UserPreferences(default_llm_model="vendor/retaining"))
        self.assertEqual(saved["default_llm_model"], "vendor/retaining")

    def test_claude_subscription_chats_store_no_flag(self) -> None:
        isolate_providers(self, claude=CLAUDE_LOGGED_IN)
        model = self.service.set_chat_model("user-one", "agent-one", self.chat, "claude_subscription:sonnet")
        self.assertEqual(model, {
            "provider": "claude_subscription", "id": "claude_subscription:sonnet", "zdr": False, "require_zdr": False,
        })
        self.assertNotIn("zdr", self.sidecar().read_text(encoding="utf-8"))


class ChatModelWithRequiredZdrTests(ModelFixture):
    def test_claude_subscription_chat_is_stored_as_not_zdr(self) -> None:
        isolate_providers(self, claude=CLAUDE_LOGGED_IN)
        model = self.service.set_chat_model("user-one", "agent-one", self.chat, "claude_subscription:sonnet")
        self.assertEqual(model["zdr"], False)
        self.assertIs(self.sources[("user-one", "agent-one")].model_selection(self.chat).zdr, False)

    def test_opting_out_stores_the_exception_and_following_the_setting_stores_nothing(self) -> None:
        self.service.set_chat_model("user-one", "agent-one", self.chat, "vendor/alpha")
        self.assertIsNone(self.sources[("user-one", "agent-one")].model_selection(self.chat).zdr)
        self.service.set_chat_model("user-one", "agent-one", self.chat, "vendor/alpha", zdr=False)
        self.assertIs(self.sources[("user-one", "agent-one")].model_selection(self.chat).zdr, False)


class ChatListTests(ModelFixture):
    def test_chat_list_spans_all_agents_and_labels_each_chat(self) -> None:
        other = self.service.create_chat("user-one", "agent-two")["id"]
        list(self.service.send("user-one", "agent-two", other, "Hello"))  # unused empty chats are pruned
        chats = self.service.list_all_chats("user-one", keep=self.chat)
        self.assertEqual({(chat["agent_id"], chat["id"]) for chat in chats}, {
            ("agent-one", self.chat), ("agent-two", other),
        })
        self.assertEqual({chat["id"]: chat["agent_name"] for chat in chats}, {self.chat: "agent-one", other: "agent-two"})

    def test_chat_list_endpoint_returns_every_agent_for_the_user(self) -> None:
        other = self.service.create_chat("user-one", "agent-two")["id"]
        list(self.service.send("user-one", "agent-two", other, "Hello"))
        with TestClient(create_app(self.service)) as client:
            login(client, "user-one")
            response = client.get(f"/api/chats?keep={self.chat}")
        self.assertEqual(response.status_code, 200)
        self.assertEqual({chat["id"] for chat in response.json()}, {self.chat, other})


    def test_rename_chat_updates_the_listed_title(self) -> None:
        list(self.service.send("user-one", "agent-one", self.chat, "Hello"))
        summary = self.service.rename_chat("user-one", "agent-one", self.chat, "Weekly plan")
        self.assertEqual(summary["title"], "Weekly plan")
        self.assertEqual(self.service.get_chat("user-one", "agent-one", self.chat)["chat"]["title"], "Weekly plan")
        with self.assertRaises(InputError):
            self.service.rename_chat("user-one", "agent-one", self.chat, "  ")
        with self.assertRaises(UnknownChat):
            self.service.rename_chat("user-one", "agent-one", "missing", "Title")


class ChatReasoningEffortServiceTests(ModelFixture):
    def setUp(self) -> None:
        super().setUp()
        self.catalog_models[0].parameter_routes = [["reasoning_effort"]]

    def test_override_is_per_chat_and_applied_to_the_agent(self) -> None:
        list(self.service.send("user-one", "agent-one", self.chat, "Question"))  # empty chats are pruned on create
        other = self.service.create_chat("user-one", "agent-one")["id"]
        chat = self.service.set_chat_reasoning_effort("user-one", "agent-one", self.chat, "high")
        self.assertEqual(chat["reasoning_effort"]["override"], "high")
        self.assertEqual(chat["reasoning_effort"]["effective"], "high")
        self.assertEqual(chat["reasoning_effort"]["default"], "medium")
        self.assertIsNone(self.service.get_chat("user-one", "agent-one", other)["reasoning_effort"]["override"])
        list(self.service.send("user-one", "agent-one", self.chat, "Question"))
        self.assertEqual(self.agents[("user-one", "agent-one")].config.reasoning_effort, "high")
        list(self.service.send("user-one", "agent-one", other, "Question"))
        self.assertEqual(self.agents[("user-one", "agent-one")].config.reasoning_effort, "medium")

    def test_clearing_restores_the_agent_default(self) -> None:
        self.service.set_chat_reasoning_effort("user-one", "agent-one", self.chat, "low")
        chat = self.service.set_chat_reasoning_effort("user-one", "agent-one", self.chat, None)
        self.assertIsNone(chat["reasoning_effort"]["override"])
        self.assertEqual(chat["reasoning_effort"]["effective"], "medium")
        self.assertIsNone(self.sources[("user-one", "agent-one")].reasoning_effort_selection(self.chat))

    def test_changing_to_an_unsupported_model_clears_the_effort_override(self) -> None:
        self.service.set_chat_reasoning_effort("user-one", "agent-one", self.chat, "high")
        self.service.set_chat_model("user-one", "agent-one", self.chat, "vendor/beta")
        chat = self.service.get_chat("user-one", "agent-one", self.chat)
        self.assertIsNone(chat["reasoning_effort"]["override"])
        self.assertEqual(chat["reasoning_effort"]["supported"], False)

    def test_unsupported_models_reject_a_value(self) -> None:
        self.service.set_chat_model("user-one", "agent-one", self.chat, "vendor/beta")
        with self.assertRaises(InputError):
            self.service.set_chat_reasoning_effort("user-one", "agent-one", self.chat, "high")
        self.assertIsNone(self.service.get_chat("user-one", "agent-one", self.chat)["reasoning_effort"]["override"])

    def test_support_is_reported_as_unknown_when_catalog_is_unavailable(self) -> None:
        self.catalog.side_effect = CatalogUnavailable("retry later")
        self.service._reasoning_support.clear()
        self.assertIsNone(self.service.get_chat_models("user-one", "agent-one", self.chat)["reasoning_effort"]["supported"])

    def test_invalid_stored_effort_is_an_explicit_error(self) -> None:
        path = self.root / "user-one" / "agent-one" / f"{self.chat}.effort.json"
        path.write_text('{"effort": "ultra"}', encoding="utf-8")
        with self.assertRaises(ChatMetadataError):
            self.service.get_chat_models("user-one", "agent-one", self.chat)


if __name__ == "__main__":
    unittest.main()
