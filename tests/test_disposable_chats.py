from __future__ import annotations

import os
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock, patch

from fastapi.testclient import TestClient

import test_service_steering as steering
from auth_helpers import login
from src.agent.agent import Agent
from src.agent.tool_groups import DISPOSABLE_BLOCKED_TOOLS, TOOL_GROUP_TOOLS
from src.config.models import AgentTerminalBackend, AgentTerminalConfig, ModelsConfig, ResolvedAgent, SshBackendConfig
from src.config.preferences import UserPreferences
from src.core.review import ReviewQueue
from src.core.sources.conversations import DEFAULT_DISPOSABLE_RETENTION_DAYS
from src.core.sources.registry import SourceRegistry
from src.providers.provider import LLMProvider
from src.utils.i18n import InputError
from src.web.app import create_app

TERMINAL_TOOLS = {
    "terminal", "sandbox_read", "sandbox_write", "sandbox_patch", "sandbox_search",
    "process_start", "process_list", "process_read", "process_wait", "process_stop",
}
WRITE_TOOLS = {"write", "append", "edit", "move", "delete", "make_pdf", "memory", "skill_manage", "soul_edit"}


def _age(source, chat_id: str, days: float) -> None:
    path = source.resolve(f"{chat_id}.jsonl")
    stamp = (datetime.now() - timedelta(days=days)).timestamp()
    os.utime(path, (stamp, stamp))


class DisposableStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.sources = SourceRegistry.for_agent("u", "a", data_dir=self.root)
        self.source = self.sources.conversations()

    def test_flag_is_a_sidecar_that_is_not_a_chat_and_converts_both_ways(self) -> None:
        chat = self.source.create(disposable=True)
        self.assertTrue(self.source.is_disposable(chat))
        self.assertTrue((self.source.root / f"{chat}.disposable.json").is_file())
        self.assertEqual(self.source.chats(), [chat])
        self.assertEqual(self.source.disposable_chats(), {chat})
        self.source.set_disposable(chat, False)
        self.assertFalse(self.source.is_disposable(chat))
        self.source.set_disposable(chat, True)
        self.assertTrue(self.source.is_disposable(chat))
        self.assertFalse(self.source.is_disposable(self.source.create()))
        with self.assertRaises(FileNotFoundError):
            self.source.set_disposable("20200101-000000-aaaaaa", True)

    def test_delete_chat_removes_every_sidecar(self) -> None:
        chat = self.source.create(disposable=True)
        self.source.append(chat, "user", "hi")
        self.source.rename(chat, "Title")
        self.source.truncate_from(chat, 0)
        self.source.delete_chat(chat)
        self.assertEqual(list(self.source.root.iterdir()), [])

    def test_summary_reports_disposable_and_expiry_from_last_activity(self) -> None:
        normal, disposable = self.source.create(), self.source.create(disposable=True)
        self.source.append(disposable, "user", "hi")
        _age(self.source, disposable, 10)
        summary = self.source.summary(disposable, 30)
        self.assertTrue(summary["disposable"])
        expires = datetime.fromisoformat(summary["expires_at"])
        expected = self.source.last_activity(disposable) + timedelta(days=30)
        self.assertLess(abs((expires - expected).total_seconds()), 1)
        self.assertGreater(expires, datetime.now().astimezone() + timedelta(days=19))
        self.assertLess(expires, datetime.now().astimezone() + timedelta(days=21))
        plain = self.source.summary(normal)
        self.assertFalse(plain["disposable"])
        self.assertIsNone(plain["expires_at"])

    def test_expired_disposable_never_includes_normal_chats(self) -> None:
        old_normal, old_disposable = self.source.create(), self.source.create(disposable=True)
        fresh_disposable = self.source.create(disposable=True)
        for chat in (old_normal, old_disposable):
            _age(self.source, chat, 45)
        self.assertEqual(self.source.expired_disposable(30), [old_disposable])
        self.assertEqual(self.source.expired_disposable(60), [])
        self.assertEqual(self.source.expired_disposable(30, datetime.now() + timedelta(days=31)),
                         sorted([old_disposable, fresh_disposable]))


class DisposableReviewAndIndexTests(DisposableStoreTests):
    def test_dream_skips_disposable_chats_and_includes_them_after_conversion(self) -> None:
        queue = ReviewQueue(self.source, self.sources.dream_state())
        normal, disposable = self.source.create(), self.source.create(disposable=True)
        for chat in (normal, disposable):
            self.source.append(chat, "user", "q")
            self.source.append(chat, "assistant", "a")
        self.assertEqual([item["chat_id"] for item in queue.pending()], [normal])
        with self.assertRaises(InputError):
            queue.read(disposable)
        self.source.set_disposable(disposable, False)
        self.assertEqual({item["chat_id"] for item in queue.pending()}, {normal, disposable})
        self.assertEqual(queue.read(disposable)["total_turns"], 2)
        self.source.set_disposable(disposable, True)
        self.assertEqual([item["chat_id"] for item in queue.pending()], [normal])


class DisposableIndexTests(unittest.TestCase):
    def test_index_excludes_disposable_chats_and_drops_converted_ones(self) -> None:
        import index_helpers
        from src.config.models import IndexConfig
        from src.core.index import VectorIndex, close_index

        directory = TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(directory.cleanup)
        data = Path(directory.name) / "data"
        sources = SourceRegistry.for_agent("u", "a", data_dir=data)
        chats = sources.conversations()
        normal, disposable = chats.create(), chats.create(disposable=True)
        for chat in (normal, disposable):
            chats.append(chat, "user", "Where should we travel?")
            chats.append(chat, "assistant", "Lisbon has great food.")
        path = data / "u" / "a" / ".index"
        index = VectorIndex(path, sources, index_helpers.FakeEmbeddings(), "fake/embed", IndexConfig(
            chunk_tokens=120, max_chunk_tokens=200, overlap_tokens=20, folders_reconcile_minutes=10))
        self.addCleanup(close_index, path)
        index.sync()
        self.assertEqual(index.status()["chats"], 1)
        index.after_turn(disposable)  # a turn of a disposable chat is not indexed either
        self.assertTrue(index.wait(10))
        self.assertEqual(index.status()["chats"], 1)
        chats.set_disposable(normal, True)  # converted: its chunks go on the next reconcile
        index.sync()
        self.assertEqual(index.status()["chats"], 0)
        chats.set_disposable(disposable, False)  # converted back: indexed again
        index.sync()
        self.assertEqual(index.status()["chats"], 1)


class DisposableToolTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.sources = SourceRegistry.for_agent("u", "a", data_dir=Path(directory.name))
        patcher = patch("src.agent.agent.VectorIndex.for_agent")
        patcher.start()
        self.addCleanup(patcher.stop)

    def _agent(self, chat_id: str | None, groups=("memory_read", "memory_edit", "self"), **extra) -> Agent:
        config = ResolvedAgent(
            user_id="u", id="a", name="Aria", models=ModelsConfig(chat="vendor/chat"), provider_name="ollama",
            api_key="", embeddings_provider_name="ollama", embeddings_api_key="", tools=True,
            tool_groups=list(groups), has_memory=True, associative_memory=False, **extra,
        )
        return Agent(config, Mock(spec=LLMProvider), chat_id, sources=self.sources)

    @staticmethod
    def _names(agent: Agent) -> set[str]:
        return {spec["function"]["name"] for spec in agent._tool_specs}

    def test_write_tools_are_absent_in_a_disposable_chat_and_present_after_conversion(self) -> None:
        chats = self.sources.conversations()
        disposable, normal = chats.create(disposable=True), chats.create()
        locked, open_ = self._agent(disposable), self._agent(normal)
        self.assertFalse(WRITE_TOOLS & self._names(locked))
        self.assertTrue({"query_memory", "read", "search", "skill_view"} <= self._names(locked))
        self.assertTrue(WRITE_TOOLS <= self._names(open_))
        self.assertTrue(WRITE_TOOLS & set(locked._tool_handlers) == set())
        chats.set_disposable(disposable, False)  # the service drops the cached agent; a new one is built
        self.assertTrue(WRITE_TOOLS <= self._names(self._agent(disposable)))

    def test_folder_write_tools_are_blocked_too(self) -> None:
        self.assertEqual(
            DISPOSABLE_BLOCKED_TOOLS,
            WRITE_TOOLS | {"folder_write", "folder_append", "folder_edit", "folder_move", "folder_delete"}
            | TERMINAL_TOOLS | {"task_start", "task_list", "task_result", "task_steer", "task_send", "task_stop", "task_wait"}  # a disposable chat cannot launch tasks either
            | set(TOOL_GROUP_TOOLS["permissions"])  # nor change anyone's permissions
            | set(TOOL_GROUP_TOOLS["agents"])  # nor create, change or delete agents
            | set(TOOL_GROUP_TOOLS["notify"])  # nor write to the user's phone
            | set(TOOL_GROUP_TOOLS["files_send"])  # nor hand files to the user
            | set(TOOL_GROUP_TOOLS["automations"]),  # nor schedule anything
        )

    def test_a_disposable_chat_never_builds_the_terminal_tools(self) -> None:
        chats = self.sources.conversations()
        extra = {
            "terminal": AgentTerminalConfig(backends={"box": AgentTerminalBackend()}),
            "terminal_backends": {"box": SshBackendConfig(
                type="ssh", host="192.0.2.10", user="agent", key_file="/k/id", known_hosts_file="/k/kh",
            )},
        }
        groups = ("memory_read", "terminal")
        open_ = self._agent(chats.create(), groups, **extra)
        self.assertTrue(TERMINAL_TOOLS <= self._names(open_))  # the control: a normal chat does get them
        locked = self._agent(chats.create(disposable=True), groups, **extra)
        self.assertFalse(TERMINAL_TOOLS & self._names(locked))
        self.assertFalse(TERMINAL_TOOLS & set(locked._tool_handlers))

    def test_handler_level_refusal_and_open_chat_switch(self) -> None:
        chats = self.sources.conversations()
        disposable, normal = chats.create(disposable=True), chats.create()
        agent = self._agent(normal)
        agent.open_chat(disposable)  # same agent object moves to a disposable chat: tools are rebuilt
        self.assertFalse(WRITE_TOOLS & self._names(agent))
        agent._tool_handlers["write"] = Mock(return_value="written")  # even if a handler slipped through
        actions = agent._run_tools([{"id": "1", "name": "write", "arguments": {"path": "x.md", "content": "y"}}])
        self.assertTrue(actions[0]["error"])
        agent._tool_handlers["write"].assert_not_called()
        agent.open_chat(normal)
        self.assertTrue(WRITE_TOOLS <= self._names(agent))

    def test_system_prompt_has_the_notice_only_for_disposable_chats(self) -> None:
        chats = self.sources.conversations()
        locked, open_ = self._agent(chats.create(disposable=True)), self._agent(chats.create())
        self.assertIn("disposable chat", locked.messages[0]["content"])
        self.assertIn("convert this chat to a normal one", locked.messages[0]["content"])
        self.assertNotIn("disposable chat", open_.messages[0]["content"])


class DisposableServiceTests(steering.SteeringFixture):
    def setUp(self) -> None:
        super().setUp()
        self.key = ("user-one", "agent-one")
        self.source = self.sources[self.key]
        self.client = TestClient(create_app(self.service))
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        login(self.client, "user-one")
        self.service._all_agents = lambda: [self.key]  # the fixture has no agent files to scan
        self.service._agent_index = Mock()  # no real index in these tests

    def _chat(self, *, disposable: bool) -> str:
        chat = self.service.create_chat("user-one", "agent-one", disposable=disposable)["id"]
        self.source.append(chat, "user", "q")  # an empty chat would be pruned by the next listing or creation
        self.source.append(chat, "assistant", "a")
        return chat

    def test_api_creates_disposable_chats_and_stays_backward_compatible(self) -> None:
        plain = self.client.post("/api/agents/agent-one/chats")
        self.assertEqual(plain.status_code, 201)
        self.assertFalse(plain.json()["disposable"])
        made = self.client.post("/api/agents/agent-one/chats", json={"disposable": True})
        self.assertEqual(made.status_code, 201)
        body = made.json()
        self.assertTrue(body["disposable"])
        self.assertTrue(body["expires_at"])
        self.source.append(body["id"], "user", "q")
        by_query = self.client.post("/api/agents/agent-one/chats?disposable=true")
        self.assertTrue(by_query.json()["disposable"])
        keep = by_query.json()["id"]
        listed = {c["id"]: c for c in self.client.get("/api/agents/agent-one/chats", params={"keep": keep}).json()}
        self.assertTrue(listed[body["id"]]["disposable"])
        self.assertTrue(listed[keep]["disposable"])
        every = {c["id"]: c for c in self.client.get("/api/chats", params={"keep": keep}).json()}
        self.assertTrue(every[body["id"]]["disposable"])
        self.assertEqual(self.client.get(f"/api/agents/agent-one/chats/{body['id']}").json()["chat"]["disposable"], True)

    def test_convert_both_ways_drops_the_cached_agent(self) -> None:
        list(self._send("Hello"))
        slot = self.service._slot(*self.key)
        self.assertIn(self.chat, slot.agents)
        url = f"/api/agents/agent-one/chats/{self.chat}/disposable"
        response = self.client.post(url, json={"disposable": True})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["disposable"])
        self.assertTrue(self.source.is_disposable(self.chat))
        self.assertNotIn(self.chat, slot.agents)
        list(self._send("Again"))
        self.assertIn("disposable chat", self.agents[self.key].messages[0]["content"])
        back = self.client.post(url, json={"disposable": False})
        self.assertFalse(back.json()["disposable"])
        self.assertIsNone(back.json()["expires_at"])
        self.assertFalse(self.source.is_disposable(self.chat))
        self.assertEqual(self.client.post(url, json={"disposable": "yes"}).status_code, 422)
        self.assertEqual(
            self.client.post("/api/agents/agent-one/chats/20200101-000000-aaaaaa/disposable", json={"disposable": True}).status_code,
            404,
        )

    def test_convert_is_refused_while_a_reply_is_running(self) -> None:
        started, release = self._gate("Slow")
        events = self._send("Slow")
        self.assertTrue(started.wait(2))
        response = self.client.post(f"/api/agents/agent-one/chats/{self.chat}/disposable", json={"disposable": True})
        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["code"], "agent_busy")
        self.assertFalse(self.source.is_disposable(self.chat))
        release.set()
        list(events)

    def test_cleanup_deletes_only_expired_idle_disposable_chats(self) -> None:
        service = self.service
        expired, recent, busy = (self._chat(disposable=True) for _ in range(3))
        old_normal = self._chat(disposable=False)
        for chat in (expired, old_normal, busy):
            _age(self.source, chat, 40)
        _age(self.source, recent, 5)
        slot = service._slot(*self.key)
        with slot.state:
            slot.active[busy] = Mock()
        self.assertEqual(service.cleanup_disposable_chats(), 1)
        remaining = set(self.source.chats())
        self.assertNotIn(expired, remaining)
        self.assertLessEqual({recent, old_normal, busy}, remaining)
        with slot.state:
            slot.active.pop(busy)

    def test_listing_chats_applies_the_cleanup_lazily(self) -> None:
        chat = self._chat(disposable=True)
        _age(self.source, chat, 31)
        ids = [c["id"] for c in self.service.list_chats("user-one", "agent-one", self.chat)]
        self.assertNotIn(chat, ids)
        self.assertFalse(any(self.source.root.glob(f"{chat}*")))

    def test_retention_preference_drives_expiry_and_is_validated(self) -> None:
        self.assertEqual(self.client.get("/api/preferences").json()["disposable_retention_days"], None)
        chat = self._chat(disposable=True)
        default = datetime.fromisoformat(self.service.get_chat("user-one", "agent-one", chat)["chat"]["expires_at"])
        response = self.client.patch("/api/preferences", json={"disposable_retention_days": 7})
        self.assertEqual(response.status_code, 200)
        short = datetime.fromisoformat(self.service.get_chat("user-one", "agent-one", chat)["chat"]["expires_at"])
        self.assertAlmostEqual((default - short).total_seconds(), 23 * 86400, delta=5)
        for bad in (0, 366, -1, 1.5, "7", True):
            self.assertEqual(self.client.patch("/api/preferences", json={"disposable_retention_days": bad}).status_code, 422, bad)
        for good in (1, 365):
            self.assertEqual(UserPreferences(disposable_retention_days=good).disposable_retention_days, good)
        self.assertEqual(DEFAULT_DISPOSABLE_RETENTION_DAYS, 30)
        _age(self.source, chat, 8)
        self.assertEqual(self.service.cleanup_disposable_chats(), 1)


if __name__ == "__main__":
    unittest.main()
