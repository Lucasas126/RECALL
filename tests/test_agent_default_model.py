from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError

import test_chat_models as models_tests
from src.config.agents import AgentStore
from src.config.loader import user_models
from src.config.models import AgentConfig, UserConfig
from src.config.preferences import UserPreferences
from src.service.chat import AgentBusy
from src.telegram.bot import TelegramSession, _apply_panel_action, _panel_view, _Panel
from src.utils.i18n import InputError, error_message
from src.web.app import create_app


def _agent(model: str | None = None, **extra) -> AgentConfig:
    chat_config = {"model": model, **extra} if model is not None else dict(extra)
    return AgentConfig(id="aria", name="Aria", chat_config=chat_config)


class AgentDefaultModelConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.user_dir = root / "config" / "user-a"
        (self.user_dir / "agents").mkdir(parents=True)
        (self.user_dir / "user.yaml").write_text(
            "id: user-a\nname: User A\nmax_level: admin\nprovider: {name: openrouter}\nagents: [aria]\n", encoding="utf-8",
        )
        self.store = AgentStore(root / "config", root / "data")

    def test_agent_without_the_key_loads_unchanged_and_follows_the_user(self) -> None:
        path = self.user_dir / "agents" / "aria.yaml"
        path.write_text("id: aria\nname: Aria\nchat_config:\n  stream: false\n", encoding="utf-8")
        config = self.store.load("user-a", "aria")
        self.assertIsNone(config.chat_config.model)
        self.assertFalse(config.chat_config.stream)

    def test_model_reference_is_validated(self) -> None:
        for good in ("claude_subscription:sonnet", "anthropic/claude-haiku-5.5", "ollama:llama3:8b"):
            self.assertEqual(_agent(good).chat_config.model, good)
        for bad in ("claude_subscription:", "", 5):
            with self.subTest(model=bad), self.assertRaises(ValidationError):
                _agent(bad)

    def test_saving_the_default_keeps_other_keys_and_clearing_removes_only_it(self) -> None:
        path = self.user_dir / "agents" / "aria.yaml"
        path.write_text(
            "id: aria\nname: Aria\ndescription: Plans\nchat_config:\n  stream: false\n  reasoning_effort: high\n"
            "dream:\n  enabled: true\n  daily_at: '03:30'\nmemory_limits: {memory_chars: 1000}\n",
            encoding="utf-8",
        )
        original = self.store.load("user-a", "aria")
        chosen = original.model_copy(update={
            "chat_config": original.chat_config.model_copy(update={"model": "claude_subscription:sonnet"}),
        })
        self.store.update("user-a", "aria", chosen)
        saved = yaml.safe_load(path.read_text(encoding="utf-8"))
        self.assertEqual(saved["chat_config"]["model"], "claude_subscription:sonnet")
        self.assertIs(saved["chat_config"]["stream"], False)
        self.assertEqual(saved["chat_config"]["reasoning_effort"], "high")
        self.assertEqual((saved["description"], saved["dream"]["daily_at"], saved["dream"]["enabled"]),
                         ("Plans", "03:30", True))
        self.assertNotIn("memory_limits", saved)  # legacy key: ignored on load, not written back
        self.store.update("user-a", "aria", original)
        cleared = yaml.safe_load(path.read_text(encoding="utf-8"))
        self.assertNotIn("model", cleared["chat_config"])
        self.assertEqual(cleared["chat_config"]["reasoning_effort"], "high")


class DefaultModelResolutionTests(unittest.TestCase):
    @staticmethod
    def _user(require_zdr: bool = False) -> UserConfig:
        return UserConfig.model_validate({
            "id": "user-a", "name": "User A", "max_level": "admin", "provider": {"name": "openrouter"}, "agents": ["aria"],
            "models": {"chat": "vendor/user"}, "require_zdr": require_zdr,
        })

    @staticmethod
    def _preferences(default: str | None = None) -> Mock:
        store = Mock()
        store.get.return_value = UserPreferences(default_llm_model=default)
        return store

    def test_agent_default_beats_the_user_default_and_unset_follows_it(self) -> None:
        user = self._user()
        self.assertEqual(user_models(user, self._preferences(), _agent()).chat, "vendor/user")
        self.assertEqual(user_models(user, self._preferences("vendor/saved"), _agent()).chat, "vendor/saved")
        self.assertEqual(user_models(user, self._preferences("vendor/saved"), _agent("vendor/agent")).chat, "vendor/agent")
        self.assertEqual(user_models(user, self._preferences("vendor/saved")).chat, "vendor/saved")

    def test_claude_subscription_agent_default_works_while_zdr_is_required(self) -> None:
        agent = _agent("claude_subscription:sonnet")
        for require_zdr in (False, True):
            self.assertEqual(user_models(self._user(require_zdr), self._preferences(), agent).chat,
                             "claude_subscription:sonnet")

class AgentDefaultModelServiceTests(models_tests.ModelFixture):
    require_zdr = False

    def setUp(self) -> None:
        super().setUp()
        self.agent_models: dict[str, str | None] = {}
        patcher = patch(
            "src.service.chat.load_agent_config",
            side_effect=lambda user, agent: _agent_named(agent, self.agent_models.get(agent)),
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def models(self, agent: str = "agent-one", chat: str | None = None) -> dict:
        return self.service.get_chat_models("user-one", agent, chat or self.chat)

    def send(self, agent: str = "agent-one", chat: str | None = None) -> str:
        list(self.service.send("user-one", agent, chat or self.chat, "Question"))
        return self.used_models[("user-one", agent)][-1]

    def test_agent_without_a_default_keeps_following_the_user(self) -> None:
        info = self.models()
        self.assertEqual((info["model"]["id"], info["model_source"], info["agent_default_model"]),
                         ("vendor/default", "user", None))
        self.assertEqual(self.send(), "vendor/default")

    def test_chat_without_selection_uses_the_agent_default(self) -> None:
        self.agent_models["agent-one"] = "vendor/alpha"
        info = self.models()
        self.assertEqual((info["model"]["id"], info["model_source"]), ("vendor/alpha", "agent"))
        self.assertEqual((info["agent_default_model"], info["user_default_model"]), ("vendor/alpha", "vendor/default"))
        self.assertIsNone(info["model_override"])
        self.assertEqual(self.send(), "vendor/alpha")
        # another agent of the same user is unaffected
        other = self.service.create_chat("user-one", "agent-two")["id"]
        self.assertEqual(self.models("agent-two", other)["model_source"], "user")

    def test_agent_default_beats_the_saved_user_default(self) -> None:
        self.service.update_preferences("user-one", UserPreferences(default_llm_model="vendor/beta"))
        self.assertEqual(self.models()["model"]["id"], "vendor/beta")
        self.agent_models["agent-one"] = "vendor/alpha"
        info = self.models()
        self.assertEqual((info["model"]["id"], info["user_default_model"]), ("vendor/alpha", "vendor/beta"))
        self.assertEqual(self.send(), "vendor/alpha")

    def test_chat_selection_beats_the_agent_default_and_clearing_falls_back_to_it(self) -> None:
        self.agent_models["agent-one"] = "vendor/alpha"
        self.service.set_chat_model("user-one", "agent-one", self.chat, "vendor/beta")
        info = self.models()
        self.assertEqual((info["model"]["id"], info["model_source"]), ("vendor/beta", "chat"))
        self.assertEqual(self.send(), "vendor/beta")
        self.service.set_chat_model("user-one", "agent-one", self.chat, None)
        info = self.models()
        self.assertEqual((info["model"]["id"], info["model_source"], info["model_override"]),
                         ("vendor/alpha", "agent", None))
        self.assertEqual(self.send(), "vendor/alpha")

    def test_clearing_without_an_agent_default_falls_back_to_the_user_default(self) -> None:
        self.service.set_chat_model("user-one", "agent-one", self.chat, "vendor/beta")
        self.service.set_chat_model("user-one", "agent-one", self.chat, None)
        info = self.models()
        self.assertEqual((info["model"]["id"], info["model_source"]), ("vendor/default", "user"))

    def test_old_chat_without_selection_follows_a_later_agent_default(self) -> None:
        self.assertEqual(self.send(), "vendor/default")
        self.agent_models["agent-one"] = "vendor/beta"
        self.assertEqual(self.send(), "vendor/beta")  # the cached agent picks it up on the next turn
        self.agent_models["agent-one"] = None
        self.assertEqual(self.send(), "vendor/default")

    def test_a_claude_agent_default_replies_while_the_user_requires_zdr(self) -> None:
        self.service._user = Mock(return_value=self._user("user-one"))
        self.service._user.return_value.require_zdr = True
        self.agent_models["agent-one"] = "claude_subscription:sonnet"
        info = self.models()
        self.assertEqual((info["model"]["id"], info["model_source"], info["model"]["zdr"]),
                         ("claude_subscription:sonnet", "agent", False))
        user = self.service._user.return_value
        resolved = user_models(user, self.service._preferences, _agent("claude_subscription:sonnet"))
        self.assertEqual(resolved.chat, "claude_subscription:sonnet")  # no policy error on the reply path

    def test_saving_a_default_validates_provider_model_and_policy(self) -> None:
        user = self._user("user-one")
        checked = self.service._check_agent_model(user, _agent("vendor/alpha"), None)
        self.assertEqual(checked.chat_config.model, "vendor/alpha")
        with self.assertRaises(ValueError):  # not in the catalog
            self.service._check_agent_model(user, _agent("vendor/missing"), None)
        # an unchanged value is not re-checked, so a retired model never blocks saving other settings
        unchanged = self.service._check_agent_model(user, _agent("vendor/missing", stream=False), "vendor/missing")
        self.assertEqual(unchanged.chat_config.model, "vendor/missing")
        self.assertIsNone(self.service._check_agent_model(user, _agent(), "vendor/alpha").chat_config.model)
        user.require_zdr = True
        with self.assertRaises(ValueError):  # no ZDR route under the user's policy
            self.service._check_agent_model(user, _agent("vendor/retaining"), None)

    def test_models_endpoint_exposes_source_and_agent_default(self) -> None:
        self.agent_models["agent-one"] = "vendor/alpha"
        with TestClient(create_app(self.service), raise_server_exceptions=False) as client:
            self.assertEqual(client.post(
                "/api/login", json={"user_id": "user-one", "password": "test-password-123"},
            ).status_code, 200)
            body = client.get(f"/api/agents/agent-one/chats/{self.chat}/models").json()
            self.assertEqual((body["model_source"], body["agent_default_model"], body["model"]["id"]),
                             ("agent", "vendor/alpha", "vendor/alpha"))
            self.assertEqual(client.put(
                f"/api/agents/agent-one/chats/{self.chat}/model", json={"model_id": "vendor/beta"},
            ).status_code, 200)
            body = client.get(f"/api/agents/agent-one/chats/{self.chat}/models").json()
            self.assertEqual((body["model_source"], body["agent_default_model"]), ("chat", "vendor/alpha"))


def _agent_named(agent_id: str, model: str | None) -> AgentConfig:
    return AgentConfig(id=agent_id, name=agent_id, chat_config={"model": model})


class TelegramAgentDefaultTests(unittest.TestCase):
    def setUp(self) -> None:
        self.service = Mock()
        self.service.require_zdr.return_value = False
        self.service.get_chat_models.return_value = {
            "model": {"id": "vendor/alpha"}, "model_override": None,
            "model_source": "agent", "agent_default_model": "vendor/alpha",
        }
        self.service.get_agent.return_value = {"chat_config": {"model": "vendor/alpha"}}
        self.panel = _Panel(
            token="t", chat_id=1, view="chat_llm", agent_id="a", target_chat="c",
            items=[{"id": "vendor/alpha", "name": "Alpha", "pricing": {}}],
        )

    def test_panel_labels_the_inherit_entry_and_the_source(self) -> None:
        session = TelegramSession("u", "a", "c")
        text, markup = _panel_view(self.service, session, self.panel)
        self.assertTrue(text.startswith("Agent default"))
        self.assertEqual(markup.inline_keyboard[0][0].text, "Use agent default")
        spanish, markup = _panel_view(self.service, TelegramSession("u", "a", "c", language="es"), self.panel)
        self.assertEqual(markup.inline_keyboard[0][0].text, "Usar el modelo por defecto del agente")
        self.service.get_chat_models.return_value = {
            "model": {"id": "vendor/default"}, "model_override": None,
            "model_source": "user", "agent_default_model": None,
        }
        text, markup = _panel_view(self.service, session, self.panel)
        self.assertTrue(text.startswith("User default"))
        self.assertEqual(markup.inline_keyboard[0][0].text, "Use user default")

    def test_inheriting_clears_the_chat_selection_and_names_the_fallback(self) -> None:
        session = TelegramSession("u", "a", "c")
        text = _apply_panel_action(self.service, session, self.panel, "inherit")
        self.service.set_chat_model.assert_called_once_with("u", "a", "c", None)
        self.assertEqual(text, "This chat now uses the agent's default LLM model.")
        self.service.get_agent.return_value = {"chat_config": {"model": None}}
        text = _apply_panel_action(self.service, session, self.panel, "inherit")
        self.assertEqual(text, "This chat now uses the user's default LLM model.")

    def test_user_default_panel_mentions_the_agent_default_but_cannot_edit_it(self) -> None:
        self.service.get_preferences.return_value = {"default_llm_model": "vendor/user"}
        panel = _Panel(token="t", chat_id=1, view="llm", agent_id="a", items=[], favorites=set())
        text, _ = _panel_view(self.service, TelegramSession("u", "a", "c"), panel)
        self.assertIn("The active agent has its own default model (vendor/alpha)", text)
        self.service.update_preferences.reset_mock()
        _apply_panel_action(self.service, TelegramSession("u", "a", "c"), _with_items(panel), "pick:0")
        saved = self.service.update_preferences.call_args.args[1]
        self.assertEqual(saved.default_llm_model, "vendor/user2")  # still the user default, never the agent's
        self.service.update_agent.assert_not_called()


def _with_items(panel: _Panel) -> _Panel:
    panel.items = [{"id": "vendor/user2", "name": "User 2"}]
    return panel


if __name__ == "__main__":
    unittest.main()
