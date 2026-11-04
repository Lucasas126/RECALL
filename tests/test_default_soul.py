from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import yaml
from fastapi.testclient import TestClient

from auth_helpers import TEST_PASSWORD_HASH, login
from src.agent.default_soul import default_soul
from src.agent.tool_groups import TOOL_GROUP_TOOLS
from src.service.chat import ChatService
from src.web.app import create_app


class DefaultSoulTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        self.config_dir, self.data_dir = root / "config", root / "data"
        agents = self.config_dir / "ana" / "agents"
        agents.mkdir(parents=True)
        (self.config_dir / "ana" / "user.yaml").write_text(
            f"id: ana\nname: Ana\nprovider: {{name: ollama}}\nagents: [old]\npassword_hash: '{TEST_PASSWORD_HASH}'\n",
            encoding="utf-8",
        )
        (agents / "old.yaml").write_text(yaml.safe_dump({"id": "old", "name": "Old"}), encoding="utf-8")
        for target in ("src.config.loader.CONFIG_DIR", "src.service.chat.CONFIG_DIR"):
            patcher = patch(target, self.config_dir)
            patcher.start()
            self.addCleanup(patcher.stop)
        for target in ("src.config.loader.DATA_DIR", "src.service.chat.DATA_DIR", "src.core.sources.registry.DATA_DIR"):
            patcher = patch(target, self.data_dir)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.service = ChatService(agent_factory=lambda *a, **k: None)
        self.client = TestClient(create_app(self.service))
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        login(self.client, "ana")

    def soul_path(self, agent: str) -> Path:
        return self.data_dir / "ana" / agent / "SOUL.md"

    def test_new_agent_gets_the_template_and_it_is_the_first_history_version(self) -> None:
        self.assertEqual(self.client.post("/api/agents", json={"id": "new", "name": "New"}).status_code, 201)
        self.assertEqual(self.soul_path("new").read_text(encoding="utf-8"), default_soul())
        self.assertEqual(self.client.get("/api/agents/new/soul/history").json(), [])
        self.client.put("/api/agents/new/soul", json={"content": "mine"})
        history = self.client.get("/api/agents/new/soul/history").json()
        self.assertEqual(len(history), 1)
        version = self.client.get(f"/api/agents/new/soul/history/{history[0]['version']}").json()
        self.assertEqual(version["content"], default_soul())

    def test_existing_agent_is_not_modified(self) -> None:
        self.assertFalse(self.soul_path("old").exists())
        self.assertEqual(self.client.get("/api/agents/old/soul").json()["content"], "")

    def test_existing_soul_in_a_reused_folder_is_kept(self) -> None:
        for agent, text in (("kept", "I am kept."), ("blank", "")):
            self.soul_path(agent).parent.mkdir(parents=True)
            self.soul_path(agent).write_text(text, encoding="utf-8")
            self.assertEqual(self.client.post("/api/agents", json={"id": agent, "name": agent}).status_code, 201)
            self.assertEqual(self.soul_path(agent).read_text(encoding="utf-8"), text)

    def test_soul_endpoint_exposes_the_template(self) -> None:
        body = self.client.get("/api/agents/old/soul").json()
        self.assertEqual(body["default"], default_soul())
        self.assertEqual(body["content"], "")


class TemplateTests(unittest.TestCase):
    def test_template_is_small_and_names_real_tools(self) -> None:
        text = default_soul()
        self.assertLessEqual(len(text), 2000)
        self.assertNotIn("\r", text)
        tools = {name for group in TOOL_GROUP_TOOLS.values() for name in group}
        for name in ("soul_edit", "memory", "skill_manage", "skill_view"):
            self.assertIn(f"`{name}`", text)
            self.assertIn(name, tools)
        self.assertIn("## About me", text)


if __name__ == "__main__":
    unittest.main()
