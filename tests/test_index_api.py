from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import yaml
from fastapi.testclient import TestClient

from auth_helpers import TEST_PASSWORD_HASH, login
from index_helpers import FakeEmbeddings
from src.agent.agent import Agent
from src.config.loader import load_agent
from src.config.models import IndexConfig, ModelsConfig, ResolvedAgent
from src.core.index import VectorIndex, close_index
from src.core.sources.registry import SourceRegistry
from src.service.chat import ChatService
from src.service.dream import DreamDependencies, DreamManager
from src.web.app import create_app
from test_dream import ScriptedProvider


class IndexApiTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config_dir = self.root / "config"
        self.data_dir = self.root / "data"
        agents = self.config_dir / "user-a" / "agents"
        agents.mkdir(parents=True)
        (self.config_dir / "user-a" / "user.yaml").write_text(
            f"id: user-a\nname: User A\nprovider: {{name: ollama}}\nagents: [aria]\npassword_hash: '{TEST_PASSWORD_HASH}'\n",
            encoding="utf-8",
        )
        self.notes = self.root / "notes"
        self.notes.mkdir()
        (self.notes / "Plan.md").write_text("# Plan\nShip the folders editor.\n", encoding="utf-8")
        (agents / "aria.yaml").write_text(yaml.safe_dump({
            "id": "aria", "name": "Aria", "folders": [{"name": "notes", "path": str(self.notes)}],
        }), encoding="utf-8")
        for target in ("src.config.loader.CONFIG_DIR", "src.service.chat.CONFIG_DIR"):
            patcher = patch(target, self.config_dir)
            patcher.start()
            self.addCleanup(patcher.stop)
        for target in ("src.config.loader.DATA_DIR", "src.service.chat.DATA_DIR", "src.core.sources.registry.DATA_DIR"):
            patcher = patch(target, self.data_dir)
            patcher.start()
            self.addCleanup(patcher.stop)
        SourceRegistry.for_agent("user-a", "aria").mind_palace().write("pets.md", "# Pets\nLuna the cat.\n")
        self.embeddings = FakeEmbeddings()
        self.index_path = self.data_dir / "user-a" / "aria" / ".index"
        self.addCleanup(close_index, self.index_path)

        def index_factory(config: ResolvedAgent, sources: SourceRegistry) -> VectorIndex:
            return VectorIndex(self.index_path, sources, self.embeddings, config.models.embeddings, IndexConfig())

        dream = DreamManager(DreamDependencies(
            resolve_agent=load_agent, provider=lambda _: ScriptedProvider([]),
            registry=lambda config: SourceRegistry.for_agent(config.user_id, config.id),
        ))
        self.service = ChatService(agent_factory=Mock(), dream=dream, index_factory=index_factory)
        self.client = TestClient(create_app(self.service))
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        login(self.client, "user-a")
        self.base = "/api/agents/aria"

    def test_status_and_reindex(self) -> None:
        status = self.client.get(f"{self.base}/index")
        self.assertEqual(status.status_code, 200, status.text)
        self.assertEqual(status.json()["chunks"], 0)
        self.assertEqual(set(status.json()["sources"]), {"mind_palace", "conversations", "agent", "notes"})
        queued = self.client.post(f"{self.base}/index/reindex")
        self.assertEqual(queued.status_code, 202, queued.text)
        self.assertTrue(VectorIndex(self.index_path, SourceRegistry.for_agent("user-a", "aria"), self.embeddings, "x").wait(20))
        done = self.client.get(f"{self.base}/index").json()
        self.assertEqual(done["files"], 2)  # mind_palace:pets.md and notes:Plan.md
        self.assertGreaterEqual(done["chunks"], 2)
        self.assertIsNotNone(done["folders_reconciled_at"])
        self.assertFalse(done["busy"])
        rebuilt = self.client.post(f"{self.base}/index/reindex", json={"full": True})
        self.assertEqual(rebuilt.status_code, 202, rebuilt.text)
        self.assertEqual(self.client.post(f"{self.base}/index/reindex", json={"full": "yes"}).status_code, 422)
        self.assertEqual(self.client.get("/api/agents/other/index").status_code, 404)
        self.client.post("/api/logout")
        self.assertEqual(self.client.get(f"{self.base}/index").status_code, 401)
        self.assertEqual(self.client.post(f"{self.base}/index/reindex").status_code, 401)

    def test_folders_are_validated_when_saving_the_agent(self) -> None:
        agent = self.client.get(self.base).json()
        self.assertEqual(agent["folders"], [{"name": "notes", "path": str(self.notes), "indexed": True, "read_only": False}])
        missing = self.client.put(self.base, json={**agent, "folders": [{"name": "gone", "path": str(self.root / "nope")}]})
        self.assertEqual(missing.status_code, 400)
        self.assertIn("folder 'gone' not found", missing.json()["detail"])
        for folders in (
            [{"name": "Bad Name", "path": str(self.notes)}],
            [{"name": "agent", "path": str(self.notes)}],
            [{"name": "x", "path": str(self.notes)}, {"name": "x", "path": str(self.notes)}],
        ):
            with self.subTest(folders=folders):
                self.assertEqual(self.client.put(self.base, json={**agent, "folders": folders}).status_code, 422)
        self.assertEqual(self.client.put(self.base, json={**agent, "vault": str(self.notes)}).status_code, 422)
        saved = self.client.put(self.base, json={
            **agent, "folders": [{"name": "notes", "path": str(self.notes), "indexed": False, "read_only": True}],
        })
        self.assertEqual(saved.status_code, 200, saved.text)
        stored = yaml.safe_load((self.config_dir / "user-a" / "agents" / "aria.yaml").read_text(encoding="utf-8"))
        self.assertEqual(stored["folders"], [{"name": "notes", "path": str(self.notes), "indexed": False, "read_only": True}])
        self.assertNotIn("notes", self.client.get(f"{self.base}/index").json()["sources"])


class AgentIndexHookTests(unittest.TestCase):
    def test_agent_reconciles_at_start_and_indexes_after_each_reply(self) -> None:
        directory = TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(directory.cleanup)
        sources = SourceRegistry.for_agent("u", "a", data_dir=Path(directory.name))
        provider = Mock()
        provider.context_window.return_value = 1000
        provider.invoke.return_value = {"response": "Hi!", "reasoning": "", "usage": None, "tool_calls": None}
        config = ResolvedAgent(
            user_id="u", id="a", name="A", models=ModelsConfig(), provider_name="ollama", api_key="",
            embeddings_provider_name="ollama", embeddings_api_key="", tools=False, associative_memory="off",
        )
        with patch("src.agent.agent.VectorIndex.for_agent") as for_agent:
            agent = Agent(config, provider, sources=sources)
        index = for_agent.return_value
        index.reconcile.assert_called_once_with()
        index.sync.assert_not_called()
        agent.note_written("mind_palace:x.md")
        self.assertEqual(agent.invoke("hello", stream=False), "Hi!")
        index.after_turn.assert_called_once_with(agent.chat_id, {"mind_palace:x.md"})
        index.query.assert_not_called()
        agent.invoke("again", stream=False)
        self.assertEqual(index.after_turn.call_args.args, (agent.chat_id, set()))


if __name__ == "__main__":
    unittest.main()
