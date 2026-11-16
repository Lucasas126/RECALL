from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

import yaml
from fastapi.testclient import TestClient

from tests.support.auth_helpers import TEST_PASSWORD_HASH, login
from src.agent.default_soul import default_soul
from src.config.loader import load_agent
from src.core.sources.registry import SourceRegistry
from src.service.chat import ChatService
from src.service.dream import DreamDependencies, DreamManager
from src.web.app import create_app
from tests.agent.dream.test_dream import ScriptedProvider


def _events(response) -> list[dict]:
    result = []
    for frame in response.text.strip().split("\n\n"):
        event = {}
        for line in frame.splitlines():
            name, value = line.split(": ", 1)
            event[name] = json.loads(value) if name == "data" else value
        result.append(event)
    return result


class DreamApiTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.config_dir = self.root / "config"
        self.data_dir = self.root / "data"
        agents = self.config_dir / "user-a" / "agents"
        agents.mkdir(parents=True)
        (self.config_dir / "user-a" / "user.yaml").write_text(
            f"id: user-a\nname: User A\nmax_level: admin\nprovider: {{name: ollama}}\nagents: [aria]\npassword_hash: '{TEST_PASSWORD_HASH}'\n",
            encoding="utf-8",
        )
        (agents / "aria.yaml").write_text(yaml.safe_dump({"id": "aria", "name": "Aria"}), encoding="utf-8")
        self.soul_file = self.data_dir / "user-a" / "aria" / "SOUL.md"
        self.soul_file.parent.mkdir(parents=True)
        self.soul_file.write_text("I am Aria.", encoding="utf-8")
        for target in (
            "src.config.loader.CONFIG_DIR", "src.service.chat.CONFIG_DIR",
        ):
            patcher = patch(target, self.config_dir)
            patcher.start()
            self.addCleanup(patcher.stop)
        for target in ("src.config.loader.DATA_DIR", "src.service.chat.DATA_DIR", "src.core.sources.registry.DATA_DIR"):
            patcher = patch(target, self.data_dir)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.provider = ScriptedProvider([])
        self.dream = DreamManager(DreamDependencies(
            resolve_agent=load_agent, provider=lambda _: self.provider,
            registry=lambda config: SourceRegistry.for_agent(config.user_id, config.id),
        ))
        self.agent_factory = Mock(side_effect=AssertionError("dream commands must not reach the model"))
        self.service = ChatService(agent_factory=self.agent_factory, dream=self.dream)
        self.client = TestClient(create_app(self.service))
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        login(self.client, "user-a")
        self.base = "/api/agents/aria"

    def test_soul_md_is_the_only_personality_source(self) -> None:
        agent = self.client.get(self.base).json()
        self.assertNotIn("personality_prompt", agent)
        agent["description"] = "edited"
        response = self.client.put(self.base, json=agent)
        self.assertEqual(response.status_code, 200, response.text)
        saved = yaml.safe_load((self.config_dir / "user-a" / "agents" / "aria.yaml").read_text(encoding="utf-8"))
        self.assertEqual(saved["description"], "edited")
        self.assertNotIn("personality_prompt", saved)
        self.assertEqual(self.soul_file.read_text(encoding="utf-8"), "I am Aria.")  # saving the agent never touches SOUL.md
        self.assertEqual(self.client.get(f"{self.base}/soul/history").json(), [])
        # the field is gone from the model: the API rejects it instead of storing it anywhere
        rejected = self.client.put(self.base, json={**agent, "personality_prompt": "I am Aria v2."})
        self.assertEqual(rejected.status_code, 422, rejected.text)
        self.assertEqual(self.client.post(
            "/api/agents", json={"id": "bea", "name": "Bea", "personality_prompt": "I am Bea."},
        ).status_code, 422)
        # a new agent starts with the default SOUL.md and its owner replaces it through the soul route
        created = self.client.post("/api/agents", json={"id": "bea", "name": "Bea"})
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual((self.data_dir / "user-a" / "bea" / "SOUL.md").read_text(encoding="utf-8"), default_soul())
        self.assertEqual(self.client.get("/api/agents/bea/soul").json()["content"], default_soul())
        self.client.put("/api/agents/bea/soul", json={"content": "I am Bea."})
        self.assertEqual((self.data_dir / "user-a" / "bea" / "SOUL.md").read_text(encoding="utf-8"), "I am Bea.")
        bea = yaml.safe_load((self.config_dir / "user-a" / "agents" / "bea.yaml").read_text(encoding="utf-8"))
        self.assertNotIn("personality_prompt", bea)

    def test_tool_group_catalog_marks_the_default_groups(self) -> None:
        groups = {group["id"]: group for group in self.client.get("/api/tool-groups").json()}
        self.assertTrue(groups["self"]["default"])
        self.assertTrue(groups["memory_read"]["default"])
        self.assertFalse(groups["folders_read"]["default"])
        self.assertFalse(groups["folders_edit"]["default"])
        self.assertNotIn("vault_read", groups)

    def test_soul_history_and_restore(self) -> None:
        self.assertEqual(self.client.get(f"{self.base}/soul").json()["content"], "I am Aria.")
        for text in ("one", "two"):
            self.assertEqual(self.client.put(f"{self.base}/soul", json={"content": text}).status_code, 200)
        history = self.client.get(f"{self.base}/soul/history").json()
        self.assertEqual(len(history), 2)
        oldest = history[-1]["version"]
        self.assertEqual(self.client.get(f"{self.base}/soul/history/{oldest}").json()["content"], "I am Aria.")
        restored = self.client.post(f"{self.base}/soul/history/{oldest}/restore").json()
        self.assertEqual(restored["content"], "I am Aria.")
        self.assertEqual(self.client.post(f"{self.base}/soul/history/20200101-000000-000000/restore").status_code, 404)
        self.assertEqual(self.client.put(f"{self.base}/soul", json={"content": "x" * 20001}).status_code, 400)

    def test_memories_round_trip_with_limit(self) -> None:
        saved = self.client.put(f"{self.base}/memories/user", json={"content": "Name: User A\n§\nLikes tea"}).json()
        self.assertEqual(saved["entries"], ["Name: User A", "Likes tea"])
        self.assertEqual(saved["limit"], 2500)
        self.assertEqual(self.client.get(f"{self.base}/memories/memory").json()["content"], "")
        self.assertEqual(self.client.put(f"{self.base}/memories/user", json={"content": "x" * 2501}).status_code, 400)
        self.assertEqual(self.client.get(f"{self.base}/memories/other").status_code, 422)

    def test_dream_settings_validation(self) -> None:
        settings = self.client.get(f"{self.base}/dream/settings").json()
        self.assertFalse(settings["dream"]["enabled"])
        self.assertEqual(settings["dream"]["tools"], ["folders"])
        self.assertNotIn("vault", settings)
        self.assertNotIn("memory_limits", settings)
        self.assertIn("## Dream session", settings["default_prompt"])
        both = {**settings["dream"], "every_hours": 6, "daily_at": "04:00"}
        self.assertEqual(self.client.put(f"{self.base}/dream/settings", json={"dream": both}).status_code, 422)
        renamed = {**settings["dream"], "tools": ["vault"]}  # the dream tool group is now `folders`
        self.assertEqual(self.client.put(f"{self.base}/dream/settings", json={"dream": renamed}).status_code, 422)
        body = {
            "dream": {**settings["dream"], "enabled": True, "daily_at": "04:00", "notify_telegram": True},
            "memory_limits": {"memory_chars": 3000, "user_chars": 1000},  # an old client may still send it: ignored
        }
        saved = self.client.put(f"{self.base}/dream/settings", json=body)
        self.assertEqual(saved.status_code, 200, saved.text)
        stored = yaml.safe_load((self.config_dir / "user-a" / "agents" / "aria.yaml").read_text(encoding="utf-8"))
        self.assertEqual(stored["dream"]["daily_at"], "04:00")
        self.assertNotIn("vault", stored)
        self.assertNotIn("memory_limits", stored)
        self.assertNotIn("memory_limits", saved.json())
        status = self.client.get(f"{self.base}/dream/status").json()
        self.assertEqual(status["schedule"]["daily_at"], "04:00")
        self.assertIsNotNone(status["schedule"]["next_run"])

    def test_start_sessions_and_detail(self) -> None:
        chat = self.client.post(f"{self.base}/chats").json()["id"]
        conversations = SourceRegistry.for_agent("user-a", "aria").conversations()
        conversations.append(chat, "user", "hello")
        self.provider.steps = [{"tool_calls": [{"id": "c1", "name": "dream_pending", "arguments": "{}"}]}, {"tool_calls": [{"id": "c2", "name": "dream_finish", "arguments": json.dumps({"summary": "All done."})}]}]
        self.assertEqual(self.client.get(f"{self.base}/dream/status").json()["pending_chats"], 1)
        started = self.client.post(f"{self.base}/dream/start")
        self.assertEqual(started.status_code, 202)
        self.assertEqual(started.json()["status"], "started")
        self.assertTrue(self.dream.wait("user-a", "aria", 5))
        sessions = self.client.get(f"{self.base}/dream/sessions").json()
        self.assertEqual(sessions[0]["status"], "completed")
        self.assertNotIn("diffs", sessions[0])
        detail = self.client.get(f"{self.base}/dream/sessions/{sessions[0]['id']}").json()
        self.assertEqual(detail["summary"]["summary"], "All done.")
        self.assertIn("tool_call", [event["type"] for event in detail["events"]])
        self.assertEqual(self.client.get(f"{self.base}/dream/sessions/20200101-000000-abcd").status_code, 404)
        self.assertEqual(self.client.get(f"{self.base}/dream/sessions/..%2Fx").status_code, 404)
        self.assertEqual(self.client.get("/api/agents/other/dream/status").status_code, 404)

    def test_max_minutes_is_saved_through_the_settings_api(self) -> None:
        settings = self.client.get(f"{self.base}/dream/settings").json()
        self.assertEqual(settings["dream"]["max_minutes"], 5)
        body = {"dream": {**settings["dream"], "max_minutes": 12}}
        self.assertEqual(self.client.put(f"{self.base}/dream/settings", json=body).json()["dream"]["max_minutes"], 12)
        body["dream"]["max_minutes"] = 0
        self.assertEqual(self.client.put(f"{self.base}/dream/settings", json=body).status_code, 422)

    def test_cancel_route_stops_the_running_session(self) -> None:
        from tests.agent.dream.test_dream_limits import BlockingProvider

        self.assertEqual(self.client.post(f"{self.base}/dream/cancel").json(), {"status": "not_running", "session_id": None})
        self.assertEqual(self.client.post("/api/agents/other/dream/cancel").status_code, 404)
        blocking = BlockingProvider(honour_token=True)
        self.addCleanup(blocking.release.set)
        self.dream._deps.provider = lambda _: blocking
        started = self.client.post(f"{self.base}/dream/start").json()
        self.assertTrue(blocking.entered.wait(5))
        self.assertTrue(self.client.get(f"{self.base}/dream/status").json()["running"])
        cancelled = self.client.post(f"{self.base}/dream/cancel")
        self.assertEqual(cancelled.status_code, 202)
        self.assertEqual(cancelled.json(), {"status": "cancelling", "session_id": started["session_id"]})
        self.assertTrue(self.dream.wait("user-a", "aria", 5))
        sessions = self.client.get(f"{self.base}/dream/sessions").json()
        self.assertEqual(sessions[0]["status"], "cancelled")
        self.assertIsNotNone(sessions[0]["timing"]["stop_requested_at"])
        status = self.client.get(f"{self.base}/dream/status").json()
        self.assertEqual((status["running"], status["cancelling"]), (False, False))

    def test_dream_cancel_command(self) -> None:
        from tests.agent.dream.test_dream_limits import BlockingProvider

        chat = self.client.post(f"{self.base}/chats").json()["id"]
        url = f"{self.base}/chats/{chat}/messages"
        idle = _events(self.client.post(url, json={"content": "/dream cancel"}))[0]["data"]
        self.assertEqual(idle, "No dream session is running for this agent.")
        blocking = BlockingProvider(honour_token=True)
        self.addCleanup(blocking.release.set)
        self.dream._deps.provider = lambda _: blocking
        self.client.post(url, json={"content": "/dream start"})
        self.assertTrue(blocking.entered.wait(5))
        status = _events(self.client.post(url, json={"content": "/dream"}))[0]["data"]
        self.assertIn("Use /dream cancel to stop the running session.", status)
        reply = _events(self.client.post(url, json={"content": "/dream cancel"}))[0]["data"]
        self.assertTrue(reply.startswith("Cancelling the dream session"))
        self.assertTrue(self.dream.wait("user-a", "aria", 5))
        self.assertIn("(cancelled)", _events(self.client.post(url, json={"content": "/dream"}))[0]["data"])
        self.agent_factory.assert_not_called()

    def test_dream_commands_in_the_chat_input_are_answered_without_the_model(self) -> None:
        chat = self.client.post(f"{self.base}/chats").json()["id"]
        url = f"{self.base}/chats/{chat}/messages"
        status = _events(self.client.post(url, json={"content": "/dream"}))
        self.assertEqual([event["event"] for event in status], ["text", "done"])
        self.assertIn("No dream sessions yet.", status[0]["data"])
        self.assertIn("Use /dream start to start a new session.", status[0]["data"])
        self.assertIn("Schedule: none (manual only).", status[0]["data"])
        self.provider.steps = [{"tool_calls": [{"id": "c1", "name": "dream_finish", "arguments": json.dumps({"summary": "Nothing to do."})}]}]
        started = _events(self.client.post(url, json={"content": "/dream start"}))
        self.assertEqual(started[0]["data"], "Nothing is pending; starting a dream session anyway.")
        self.assertTrue(self.dream.wait("user-a", "aria", 5))
        status = _events(self.client.post(url, json={"content": "/dream"}))[0]["data"]
        self.assertIn("(completed)", status)
        self.assertIn("Nothing to do.", status)
        unknown = _events(self.client.post(url, json={"content": "/dream sometimes"}))[0]["data"]
        self.assertIn("Unknown /dream option", unknown)
        self.agent_factory.assert_not_called()
        self.assertEqual(SourceRegistry.for_agent("user-a", "aria").conversations().load(chat), [])


if __name__ == "__main__":
    unittest.main()
