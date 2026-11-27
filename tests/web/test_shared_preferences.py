"""Preferences and defaults accept a person's shared agents, also when they have no agent of their own."""
from __future__ import annotations

import unittest

from src.config.preferences import UserPreferences
from tests.web.test_shared_agents_web import SHARED, SharedAgentFixture


class SharedPreferencesTests(SharedAgentFixture):
    def _drop_own_agents(self, user: str) -> None:
        path = self.config / user / "user.yaml"
        path.write_text(path.read_text(encoding="utf-8").replace("agents: [agent-one]", "agents: []"), encoding="utf-8")
        self.service._good_users.pop(user, None)

    def test_a_shared_agent_can_be_the_open_and_default_agent(self) -> None:
        body = self.service.update_preferences("user-one", UserPreferences(agent_id=SHARED, default_agent_id=SHARED))
        self.assertEqual(body["agent_id"], SHARED)
        self.assertEqual(self.service.default_agent("user-one"), SHARED)

    def test_a_member_with_only_shared_agents_gets_one_by_default(self) -> None:
        self._drop_own_agents("user-two")
        self.assertEqual(self.service.get_preferences("user-two")["agent_id"], SHARED)
        self.assertEqual(self.service.default_agent("user-two"), SHARED)

    def test_a_non_member_cannot_pick_the_shared_agent(self) -> None:
        with self.assertRaises(Exception):
            self.service.update_preferences("user-three", UserPreferences(agent_id=SHARED))

    def test_the_web_shows_no_empty_state_to_a_member_with_only_shared_agents(self) -> None:
        self._drop_own_agents("user-two")
        client = self._client_as("user-two")
        self.assertEqual(client.get("/api/preferences").json()["agent_id"], SHARED)
        created = client.post(f"/api/agents/{SHARED}/chats")
        self.assertEqual(created.status_code, 201)


if __name__ == "__main__":
    unittest.main()
