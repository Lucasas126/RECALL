"""The agent `hidden` flag: model, store round trip, API and the agent list."""

from __future__ import annotations

import unittest

import yaml
from fastapi.testclient import TestClient
from pydantic import ValidationError

from src.config.models import AgentConfig
from src.service.mentions import parse_mention
from src.web.app import create_app
from tests.support.admin_helpers import AdminFixture
from tests.support.auth_helpers import login


class HiddenAgentConfigTests(unittest.TestCase):
    def test_defaults_to_visible_and_must_be_a_boolean(self) -> None:
        self.assertFalse(AgentConfig(id="a", name="A").hidden)
        self.assertTrue(AgentConfig(id="a", name="A", hidden=True).hidden)
        with self.assertRaises(ValidationError):
            AgentConfig(id="a", name="A", hidden="maybe")


class HiddenAgentApiTests(AdminFixture):
    def setUp(self) -> None:
        super().setUp()
        self.client = TestClient(create_app(self.service))
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        login(self.client, "user-a")

    def agent_yaml(self) -> dict:
        return yaml.safe_load((self.config_dir / "user-a" / "agents" / "bot.yaml").read_text(encoding="utf-8"))

    def body(self) -> dict:
        body = self.client.get("/api/agents/bot").json()
        body.pop("avatar"), body.pop("level")
        return body

    def test_hidden_is_saved_exposed_and_kept_when_omitted(self) -> None:
        body = self.body()
        self.assertIs(body["hidden"], False)
        self.assertNotIn("hidden", self.agent_yaml())
        saved = self.client.put("/api/agents/bot", json={**body, "hidden": True})
        self.assertEqual((saved.status_code, saved.json()["hidden"]), (200, True))
        self.assertIs(self.agent_yaml()["hidden"], True)
        configs = {a["id"]: a for a in self.client.get("/api/agents").json()}
        self.assertIs(configs["bot"]["hidden"], True)
        selector = {a["id"]: a for a in self.client.get("/api/me").json()["agents"]}
        self.assertIs(selector["bot"]["hidden"], True)
        omitted = {k: v for k, v in body.items() if k != "hidden"}
        kept = self.client.put("/api/agents/bot", json={**omitted, "description": "changed"})
        self.assertEqual((kept.status_code, kept.json()["hidden"]), (200, True))
        shown = self.client.put("/api/agents/bot", json={**body, "hidden": False})
        self.assertIs(shown.json()["hidden"], False)
        self.assertNotIn("hidden", self.agent_yaml())

    def test_invalid_value_is_rejected_and_create_accepts_it(self) -> None:
        self.assertEqual(self.client.put("/api/agents/bot", json={**self.body(), "hidden": "maybe"}).status_code, 422)
        created = self.client.post("/api/agents", json={"id": "ghost", "name": "Ghost", "hidden": True})
        self.assertEqual((created.status_code, created.json()["hidden"]), (201, True))

    def test_a_hidden_agent_is_still_a_mention_target(self) -> None:
        self.client.put("/api/agents/bot", json={**self.body(), "hidden": True})
        self.client.post("/api/agents", json={"id": "other", "name": "Other"})
        self.assertEqual(parse_mention("@bot hi"), ("bot", "hi"))
        self.assertEqual(self.service._mention_target("user-a", "other", "@bot hi"), ("bot", "hi"))


if __name__ == "__main__":
    unittest.main()
