"""Permission levels: the web API exposes levels read-only for users and read-write for agents, with the level rule."""

from __future__ import annotations

import unittest

import yaml
from tests.support.admin_helpers import AdminFixture
from tests.support.auth_helpers import login
from fastapi.testclient import TestClient

from src.config.levels import (
    LEVEL_BELOW_REQUIRED, LEVEL_EXCEEDS_MAX, LEVEL_REQUIRED_ABOVE_MAX, LevelBelowRequired, LevelExceedsMax,
    LevelRequiredAboveMax,
)
from src.utils.i18n import error_message
from src.web.app import create_app


class LevelApiTests(AdminFixture):
    def setUp(self) -> None:
        super().setUp()
        self.write_user(self.read("user-a/user.yaml").replace("max_level: admin", "max_level: operator"))
        self.client = TestClient(create_app(self.service))
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        login(self.client, "user-a")

    def stored(self, name: str = "bot") -> dict:
        return yaml.safe_load(self.read(f"user-a/agents/{name}.yaml"))

    def test_login_and_me_show_the_effective_max_level(self) -> None:
        self.assertEqual(self.client.get("/api/me").json()["max_level"], "operator")
        response = self.client.post("/api/login", json={"user_id": "user-a", "password": "test-password-123"})
        self.assertEqual(response.json()["max_level"], "operator")

    def test_agent_payloads_show_the_effective_level(self) -> None:
        self.assertEqual(self.client.get("/api/agents/bot").json()["level"], "assistant")  # computed from the defaults
        self.assertEqual([a["level"] for a in self.client.get("/api/agents").json()], ["assistant"])
        self.write_agent("bot", "id: bot\nname: Bot\nlevel: Reader\n")
        self.assertEqual(self.client.get("/api/agents/bot").json()["level"], "reader")

    def test_create_and_update_accept_a_level_up_to_the_maximum(self) -> None:
        created = self.client.post("/api/agents", json={"id": "new", "name": "New", "level": "collaborator"})
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(created.json()["level"], "collaborator")
        self.assertEqual(self.stored("new")["level"], "collaborator")
        body = self.client.get("/api/agents/new").json()
        body["level"] = "OPERATOR"
        self.assertEqual(self.client.put("/api/agents/new", json=body).status_code, 200)
        self.assertEqual(self.stored("new")["level"], "operator")

    def test_a_level_above_the_maximum_is_refused_with_a_code(self) -> None:
        response = self.client.post("/api/agents", json={"id": "new", "name": "New", "level": "admin"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "level_exceeds_max")
        self.assertIn("operator", response.json()["detail"])
        self.assertFalse((self.config_dir / "user-a" / "agents" / "new.yaml").exists())
        self.assertIn("agents: [bot]", self.read("user-a/user.yaml"))
        body = self.client.get("/api/agents/bot").json()
        body["level"] = "admin"
        response = self.client.put("/api/agents/bot", json=body)
        self.assertEqual((response.status_code, response.json()["code"]), (400, "level_exceeds_max"))
        self.assertNotIn("level", self.stored())

    def test_the_error_message_has_a_spanish_translation(self) -> None:
        error = LevelExceedsMax(LEVEL_EXCEEDS_MAX, level="admin", agent_id="bot", max_level="operator")
        self.assertIn("supera tu nivel máximo", error_message("es", error))
        self.assertIn("above your maximum level", error_message("en", error))

    def test_the_level_rule_codes_over_the_api(self) -> None:
        body = {"id": "new", "name": "New", "level": "minimal", "chat_config": {"tool_groups": ["web"]}}
        response = self.client.post("/api/agents", json=body)
        self.assertEqual((response.status_code, response.json()["code"]), (400, "level_below_required"))
        self.assertIn("tool group 'web'", response.json()["detail"])
        self.assertIn("'assistant'", response.json()["detail"])
        self.write_user(self.read("user-a/user.yaml").replace("max_level: operator", "max_level: assistant"))
        body = {"id": "new", "name": "New", "chat_config": {"tool_groups": ["folders_edit"]}}
        response = self.client.post("/api/agents", json=body)
        self.assertEqual((response.status_code, response.json()["code"]), (400, "level_required_above_max"))
        self.assertIn("tool group 'folders_edit'", response.json()["detail"])
        self.assertFalse((self.config_dir / "user-a" / "agents" / "new.yaml").exists())

    def test_the_new_messages_have_spanish_translations(self) -> None:
        below = LevelBelowRequired(LEVEL_BELOW_REQUIRED, agent_id="bot", level="reader", service="tool group 'web'", needed="assistant")
        above = LevelRequiredAboveMax(
            LEVEL_REQUIRED_ABOVE_MAX, agent_id="bot", service="chat_config.mcp", needed="collaborator", max_level="assistant",
        )
        self.assertIn("necesita el nivel 'assistant'", error_message("es", below))
        self.assertIn("por encima de tu nivel máximo", error_message("es", above))
        self.assertIn("tool group 'web'", error_message("es", below))  # the service label is an opaque value

    def test_an_unknown_level_is_a_validation_error(self) -> None:
        response = self.client.post("/api/agents", json={"id": "new", "name": "New", "level": "root"})
        self.assertEqual(response.status_code, 422)

    def test_put_without_level_preserves_the_stored_level(self) -> None:
        self.write_agent("bot", "id: bot\nname: Bot\nlevel: collaborator\n")
        body = self.client.get("/api/agents/bot").json()
        del body["level"]  # the web form does not know the field
        body["description"] = "edited"
        response = self.client.put("/api/agents/bot", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["level"], "collaborator")
        self.assertEqual(self.stored()["level"], "collaborator")
        self.assertEqual(self.stored()["description"], "edited")

    def test_put_without_level_on_an_undeclared_agent_stays_undeclared(self) -> None:
        body = self.client.get("/api/agents/bot").json()
        del body["level"]
        self.assertEqual(self.client.put("/api/agents/bot", json=body).status_code, 200)
        self.assertNotIn("level", self.stored())

    def test_rename_through_put_keeps_the_level(self) -> None:
        self.write_agent("bot", "id: bot\nname: Bot\nlevel: reader\nchat_config:\n  tool_groups: [memory_read]\n")
        body = {"id": "bot2", "name": "Bot", "chat_config": {"tool_groups": ["memory_read"]}}
        self.assertEqual(self.client.put("/api/agents/bot", json=body).status_code, 200)
        self.assertEqual(self.stored("bot2")["level"], "reader")

    def test_a_user_cannot_change_their_own_max_level(self) -> None:
        before = self.read("user-a/user.yaml")
        for path in ("/api/preferences",):
            response = self.client.patch(path, json={"max_level": "admin"})
            self.assertEqual(response.status_code, 422, response.text)
        # not an agent field either: the agent body forbids unknown keys
        response = self.client.post("/api/agents", json={"id": "new", "name": "New", "max_level": "admin"})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.client.get("/api/me").json()["max_level"], "operator")
        self.assertEqual(self.read("user-a/user.yaml"), before)
        self.assertFalse(any(p.name == "preferences.json" and "admin" in p.read_text() for p in self.config_dir.rglob("*.json")))


if __name__ == "__main__":
    unittest.main()
