"""Saving an agent's configuration while it replies: allowed, except a rename (it moves the data folder)."""

from __future__ import annotations

import unittest

import yaml
from fastapi.testclient import TestClient

from tests.support.admin_helpers import AdminFixture
from tests.support.auth_helpers import login
from src.config.models import AgentConfig
from src.service.chat import AgentBusy
from src.web.app import create_app


class SaveWhileBusyTests(AdminFixture):
    def setUp(self) -> None:
        super().setUp()
        self.slot = self.service._slot("user-a", "bot")
        self.cached = object()  # the running reply's agent
        self.slot.agents["chat"] = self.cached
        self.slot.fingerprints["chat"] = self.service._runtime_fingerprint("user-a", "bot")
        self.slot.active["chat"] = object()  # a reply is running
        self.addCleanup(self.slot.active.clear)

    def groups(self) -> list[str]:
        return yaml.safe_load(self.read("user-a/agents/bot.yaml"))["chat_config"]["tool_groups"]

    def body(self, **extra) -> dict:
        return {"id": "bot", "name": "Bot", "chat_config": {"tool_groups": ["memory_read", "automations"]}, **extra}

    def test_a_non_rename_save_is_written_and_the_next_reply_rebuilds_the_agent(self) -> None:
        self.service.update_agent("user-a", "bot", AgentConfig.model_validate(self.body()))
        self.assertIn("automations", self.groups())
        self.assertIs(self.slot.agents["chat"], self.cached)  # the running reply keeps its agent
        self.assertNotEqual(self.slot.fingerprints["chat"], self.service._runtime_fingerprint("user-a", "bot"))

    def test_a_queued_reply_does_not_block_the_save_either(self) -> None:
        self.slot.active.clear()
        self.slot.pending["chat"] = object()
        self.addCleanup(self.slot.pending.clear)
        self.service.update_agent("user-a", "bot", AgentConfig.model_validate(self.body()))
        self.assertIn("automations", self.groups())

    def test_an_idle_save_still_drops_the_cached_agents(self) -> None:
        self.slot.active.clear()
        self.service.update_agent("user-a", "bot", AgentConfig.model_validate(self.body()))
        self.assertEqual(self.slot.agents, {})

    def test_a_rename_while_busy_is_refused_and_nothing_changes(self) -> None:
        before = self.read("user-a/agents/bot.yaml")
        with self.assertRaises(AgentBusy):
            self.service.update_agent("user-a", "bot", AgentConfig.model_validate(self.body(id="renamed")))
        self.assertEqual(self.read("user-a/agents/bot.yaml"), before)

    def test_the_web_route_saves_while_busy(self) -> None:
        with TestClient(create_app(self.service)) as client:
            login(client, "user-a")
            response = client.put("/api/agents/bot", json=self.body())
            self.assertEqual(response.status_code, 200, response.text)
            self.assertIn("automations", response.json()["chat_config"]["tool_groups"])
            renamed = client.put("/api/agents/bot", json=self.body(id="renamed"))
            self.assertEqual(renamed.status_code, 409, renamed.text)
        self.assertIn("automations", self.groups())


if __name__ == "__main__":
    unittest.main()
