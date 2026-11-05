from __future__ import annotations

import logging
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import requests
from pydantic import ValidationError

from src.config.models import AgentConfig, OrchestratorConfig, ProviderConfig, UserConfig
from src.config.preferences import UserPreferences
from src.providers import decisions
from src.providers.decisions import DecisionError, build_request, decide_choice
from src.service.chat import ChatService, RouteDecision, UnknownAgent

CRITERIA = {"agent-a": "Cook: recipes", "agent-b": "Tax: taxes"}
ANSWER = {
    "answers": {"agent": {
        "type": "choice", "choice": "agent-b", "probabilities": {"agent-a": 0.1, "agent-b": 0.9}, "confidence": 0.9,
    }},
    "usage": {"input_tokens": 10, "output_tokens": 1, "cost": 0.00001}, "id": "x", "provider": "p",
}


def _response(status: int = 200, body=ANSWER) -> Mock:
    response = Mock(status_code=status)
    if isinstance(body, Exception):
        response.json.side_effect = body
    else:
        response.json.return_value = body
    return response


class DecisionClientTests(unittest.TestCase):
    def ask(self, response=None, *, zdr: bool = False, session=None, **kwargs):
        session = session or Mock(post=Mock(return_value=response if response is not None else _response()))
        with patch("src.providers.decisions.http_session", return_value=session):
            return decide_choice(
                api_key="secret-key", model="typesafe/jev-1.14", state="how do I file taxes?",
                instructions="Which agent?", criteria=CRITERIA, zdr=zdr, timeout=3, **kwargs,
            ), session

    def test_exact_request_body_without_zdr(self) -> None:
        _, session = self.ask()
        args, kwargs = session.post.call_args
        self.assertEqual(args[0], "https://openrouter.ai/api/alpha/decisions")
        self.assertEqual(kwargs["json"], {
            "model": "typesafe/jev-1.14", "state": "how do I file taxes?",
            "questions": {"agent": {"type": "choice", "instructions": "Which agent?", "criteria": CRITERIA}},
        })
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer secret-key")
        self.assertEqual(kwargs["timeout"], 3)
        self.assertNotIn("provider", kwargs["json"])

    def test_zdr_is_sent_only_when_required(self) -> None:
        _, session = self.ask(zdr=True)
        self.assertEqual(session.post.call_args.kwargs["json"]["provider"], {"zdr": True})
        self.assertEqual(build_request(model="m", state="s", instructions="i", criteria={}, zdr=False).keys(), {"model", "state", "questions"})

    def test_parses_the_answer(self) -> None:
        decision, _ = self.ask()
        self.assertEqual((decision.choice, decision.confidence, decision.cost), ("agent-b", 0.9, 0.00001))
        self.assertEqual(decision.probabilities, {"agent-a": 0.1, "agent-b": 0.9})

    def test_every_failure_mode_raises_decision_error(self) -> None:
        bad = [
            _response(500, {}), _response(429, {}), _response(200, ValueError("no json")), _response(200, {}),
            _response(200, {"answers": {"agent": {"choice": "ghost", "confidence": 0.9}}}),
            _response(200, {"answers": {"agent": {"choice": "agent-a", "confidence": "high"}}}),
            _response(200, {"answers": {"agent": {"choice": "agent-a", "confidence": 3}}}),
        ]
        for response in bad:
            with self.subTest(status=response.status_code), self.assertRaises(DecisionError):
                self.ask(response)
        for exc in (requests.Timeout("slow"), requests.ConnectionError("down")):
            with self.subTest(exc=type(exc).__name__), self.assertRaises(DecisionError):
                self.ask(session=Mock(post=Mock(side_effect=exc)))
        with self.assertRaises(DecisionError):
            decide_choice(api_key="", model="m", state="s", instructions="i", criteria=CRITERIA)


class OrchestratorSettingsTests(unittest.TestCase):
    def test_defaults_are_opt_in(self) -> None:
        config = OrchestratorConfig()
        self.assertEqual(
            (config.enabled, config.model, config.api_key, config.zdr, config.min_confidence),
            (False, "typesafe/jev-1.14", "OPENROUTER_API_KEY", None, 0.6),
        )
        self.assertEqual((config.timeout_seconds, config.grace_seconds, config.uncertain_grace_seconds), (4, 8, 20))

    def test_validation_bounds(self) -> None:
        for bad in (
            {"min_confidence": 1.5}, {"min_confidence": -0.1}, {"timeout_seconds": 0}, {"timeout_seconds": 31},
            {"enabled": "yes"}, {"api_key": "not a name"}, {"model": ""}, {"unknown": 1}, {"grace_seconds": -1},
        ):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                OrchestratorConfig.model_validate(bad)
        OrchestratorConfig.model_validate({"timeout_seconds": 1, "min_confidence": 0, "zdr": True})

    def test_shipped_settings_yaml_is_valid_and_off(self) -> None:
        from src.config.settings import ORCHESTRATOR
        self.assertFalse(ORCHESTRATOR.enabled)

    def test_preference_fields(self) -> None:
        self.assertIsNone(UserPreferences().orchestrator)
        self.assertIsNone(UserPreferences().core_agent_id)
        self.assertIs(UserPreferences(orchestrator=True, core_agent_id="a_b-1").orchestrator, True)
        for bad in ({"orchestrator": "yes"}, {"orchestrator": 1}, {"core_agent_id": "no spaces"}, {"core_agent_id": ""}):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                UserPreferences.model_validate(bad)


class RouteMessageTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.users = {
            "u": UserConfig(id="u", name="u", provider=ProviderConfig(name="openrouter"), password_hash="x" * 20, agents=["agent-a", "agent-b"]),
            "solo": UserConfig(id="solo", name="solo", provider=ProviderConfig(name="openrouter"), password_hash="x" * 20, agents=["agent-a"]),
            "zdr": UserConfig(id="zdr", name="zdr", provider=ProviderConfig(name="openrouter"), password_hash="x" * 20, agents=["agent-a", "agent-b"], require_zdr=True),
        }
        for user in self.users:
            (root / user).mkdir()
        descriptions = {"agent-a": "Recipes and cooking.", "agent-b": ""}
        for target, value in (
            ("src.service.chat.CONFIG_DIR", root),
            ("src.service.chat.load_user", Mock(side_effect=lambda user: self.users[user])),
            ("src.service.chat.load_agent_config", Mock(side_effect=lambda user, agent: AgentConfig(
                id=agent, name=agent.upper(), description=descriptions[agent]))),
            ("src.service.chat.ORCHESTRATOR", OrchestratorConfig(enabled=True, min_confidence=0.6)),
        ):
            started = patch(target, value)
            started.start()
            self.addCleanup(started.stop)
        env = patch.dict("os.environ", {"OPENROUTER_API_KEY": "secret-key"})
        env.start()
        self.addCleanup(env.stop)
        self.service = ChatService(agent_factory=Mock())
        self.session = Mock(post=Mock(return_value=_response()))
        session = patch("src.providers.decisions.http_session", return_value=self.session)
        session.start()
        self.addCleanup(session.stop)

    def warned(self):
        """Context manager asserting that the service logged a warning."""
        from contextlib import contextmanager

        @contextmanager
        def check():
            with patch("src.service.chat.logger") as log:
                yield
            log.warning.assert_called()
        return check()

    def test_confident_decision_and_criteria_from_the_users_agents(self) -> None:
        decision = self.service.route_message("u", "  taxes please ")
        self.assertIsInstance(decision, RouteDecision)
        self.assertEqual((decision.agent_id, decision.confidence, decision.uncertain), ("agent-b", 0.9, False))
        self.assertEqual(decision.ranking, [("agent-b", 0.9), ("agent-a", 0.1)])
        body = self.session.post.call_args.kwargs["json"]
        self.assertEqual(body["state"], "taxes please")
        self.assertEqual(body["questions"]["agent"]["criteria"], {"agent-a": "AGENT-A: Recipes and cooking.", "agent-b": "AGENT-B"})
        self.assertNotIn("provider", body)

    def test_uncertain_below_min_confidence_and_core_agent(self) -> None:
        answer = {"answers": {"agent": {"choice": "agent-a", "probabilities": {"agent-a": 0.5, "agent-b": 0.4}, "confidence": 0.5}}}
        self.session.post.return_value = _response(200, answer)
        with patch.object(self.service._preferences, "get", return_value=UserPreferences(core_agent_id="agent-b")):
            decision = self.service.route_message("u", "hello")
        self.assertTrue(decision.uncertain)
        self.assertEqual(decision.core_agent_id, "agent-b")

    def test_zdr_follows_the_user(self) -> None:
        self.service.route_message("zdr", "hi")
        self.assertEqual(self.session.post.call_args.kwargs["json"]["provider"], {"zdr": True})

    def test_skipped_without_calling_the_api(self) -> None:
        cases = [("solo", "hi"), ("u", ""), ("u", "   "), ("u", None)]
        for user, text in cases:
            with self.subTest(user=user, text=text):
                self.assertIsNone(self.service.route_message(user, text))
        with patch("src.service.chat.ORCHESTRATOR", OrchestratorConfig(enabled=False)):
            self.assertIsNone(self.service.route_message("u", "hi"))
            self.assertFalse(self.service.orchestrator_active("u"))
        self.session.post.assert_not_called()

    def test_user_preference_overrides_the_setting(self) -> None:
        with patch("src.service.chat.ORCHESTRATOR", OrchestratorConfig(enabled=False)):
            with patch.object(self.service._preferences, "get", return_value=UserPreferences(orchestrator=True)):
                self.assertIsNotNone(self.service.route_message("u", "hi"))
        with patch.object(self.service._preferences, "get", return_value=UserPreferences(orchestrator=False)):
            self.assertIsNone(self.service.route_message("u", "hi"))

    def test_never_raises(self) -> None:
        failures = [_response(500, {}), _response(200, {"answers": {"agent": {"choice": "ghost", "confidence": 1}}}),
                    _response(200, ValueError("x"))]
        for response in failures:
            self.session.post.return_value = response
            with self.subTest(status=response.status_code), self.warned():
                self.assertIsNone(self.service.route_message("u", "hi"))
        self.session.post.side_effect = requests.Timeout("slow")
        with self.warned():
            self.assertIsNone(self.service.route_message("u", "hi"))
        self.session.post.side_effect = RuntimeError("boom")
        with self.warned():
            self.assertIsNone(self.service.route_message("u", "hi"))

    def test_missing_key_returns_none(self) -> None:
        with patch.dict("os.environ", {}, clear=True), self.warned():
            self.assertIsNone(self.service.route_message("u", "hi"))
        self.session.post.assert_not_called()

    def test_core_agent_must_exist_and_follows_renames(self) -> None:
        self.assertEqual(self.service.update_preferences("u", UserPreferences(core_agent_id="agent-b"))["core_agent_id"], "agent-b")
        self.assertEqual(self.service.orchestrator_core_agent("u"), "agent-b")
        with self.assertRaises(UnknownAgent):
            self.service.update_preferences("u", UserPreferences(core_agent_id="ghost"))
        self.service._replace_preferred_agent("u", "agent-b", None)
        self.assertIsNone(self.service.get_preferences("u")["core_agent_id"])
        self.assertIn("orchestrator_default", self.service.get_preferences("u"))
        self.service.update_preferences("u", UserPreferences(core_agent_id=None, orchestrator=True))
        self.assertTrue(self.service.get_preferences("u")["orchestrator"])

    def test_default_agent_must_exist_follows_renames_and_is_dropped_on_delete(self) -> None:
        self.assertIsNone(UserPreferences().default_agent_id)
        for bad in ({"default_agent_id": "no spaces"}, {"default_agent_id": ""}):
            with self.subTest(bad=bad), self.assertRaises(ValidationError):
                UserPreferences.model_validate(bad)
        saved = self.service.update_preferences("u", UserPreferences(default_agent_id="agent-b"))
        self.assertEqual(saved["default_agent_id"], "agent-b")
        with self.assertRaises(UnknownAgent):
            self.service.update_preferences("u", UserPreferences(default_agent_id="ghost"))
        self.assertEqual(self.service.get_preferences("u")["default_agent_id"], "agent-b")
        self.service._replace_preferred_agent("u", "agent-b", "agent-a")  # rename
        self.assertEqual(self.service.get_preferences("u")["default_agent_id"], "agent-a")
        self.service._replace_preferred_agent("u", "agent-a", None)  # delete
        self.assertIsNone(self.service.get_preferences("u")["default_agent_id"])

    def test_default_agent_accessor_falls_back_to_the_first_agent(self) -> None:
        first = self.service._user("u").agents[0]
        self.assertEqual(self.service.default_agent("u"), first)  # nothing saved
        self.service.update_preferences("u", UserPreferences(default_agent_id="agent-b"))
        self.assertEqual(self.service.default_agent("u"), "agent-b")
        with patch.object(self.service._preferences, "get", return_value=UserPreferences(default_agent_id="gone")):
            self.assertEqual(self.service.default_agent("u"), first)  # the saved agent no longer exists


if __name__ == "__main__":
    unittest.main()
