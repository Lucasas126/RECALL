from __future__ import annotations

import json
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import Mock

from fastapi.testclient import TestClient

from tests.tasks import test_service_steering as steering
from tests.support.auth_helpers import login
from src.core.sources.registry import SourceRegistry
from src.telegram.bot import TelegramSession, _prepare_message_session
from src.utils.i18n import InputError
from src.web.app import create_app


class ChatOriginStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.source = SourceRegistry.for_agent("u", "a", data_dir=Path(directory.name)).conversations()

    def test_origin_defaults_to_web_and_is_stored(self) -> None:
        web, telegram = self.source.create(), self.source.create(origin="telegram")
        self.assertEqual((self.source.origin(web), self.source.origin(telegram)), ("web", "telegram"))
        self.assertEqual(self.source.summary(telegram)["origin"], "telegram")
        self.assertEqual(self.source.summary(web)["origin"], "web")

    def test_legacy_chat_without_marker_counts_as_web(self) -> None:
        self.source.resolve("20200101-000000-aaaaaa.jsonl").write_text("", encoding="utf-8")
        self.assertEqual(self.source.origin("20200101-000000-aaaaaa"), "web")

    def test_origin_can_change_and_rejects_invalid_values(self) -> None:
        chat = self.source.create()
        self.source.set_origin(chat, "telegram")
        self.assertEqual(self.source.origin(chat), "telegram")
        for bad in ("", "Web Site", "../x", 3):
            with self.assertRaises(InputError):
                self.source.set_origin(chat, bad)
        with self.assertRaises(FileNotFoundError):
            self.source.set_origin("20200101-000000-bbbbbb", "web")

    def test_sidecar_is_not_listed_and_is_removed_on_delete(self) -> None:
        chat = self.source.create(origin="telegram")
        self.assertEqual(self.source.chats(), [chat])
        self.source.delete_chat(chat)
        self.assertEqual(list(self.source.root.iterdir()), [])


class ChatOriginServiceTests(steering.SteeringFixture):
    def setUp(self) -> None:
        super().setUp()
        self.key = ("user-one", "agent-one")
        self.source = self.sources[self.key]
        self.service._all_agents = lambda: [self.key]
        self.service._agent_index = Mock()
        self.client = TestClient(create_app(self.service))
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        login(self.client, "user-one")

    def _chat(self, origin: str, age: timedelta) -> str:
        chat = self.service.create_chat("user-one", "agent-one", origin=origin)["id"]
        ts = (datetime.now(timezone.utc) - age).isoformat(timespec="seconds")
        lines = [{"role": "user", "content": "q", "ts": ts}, {"role": "assistant", "content": "a", "ts": ts}]
        self.source.resolve(f"{chat}.jsonl").write_text("".join(json.dumps(t) + "\n" for t in lines), encoding="utf-8")
        return chat

    def test_web_api_creates_web_chats_and_lists_origin(self) -> None:
        made = self.client.post("/api/agents/agent-one/chats")
        self.assertEqual(made.json()["origin"], "web")
        self.assertEqual(self.service.create_chat("user-one", "agent-one", origin="telegram")["origin"], "telegram")

    def test_latest_chat_filters_by_origin(self) -> None:
        tele = self._chat("telegram", timedelta(hours=2))
        web = self._chat("web", timedelta(minutes=5))
        self.assertEqual(self.service.latest_chat("user-one")["id"], web)
        self.assertEqual(self.service.latest_chat("user-one", origin="telegram")["id"], tele)
        self.assertEqual({c["origin"] for c in self.service.list_all_chats("user-one")}, {"web", "telegram"})
        self.assertEqual({c["origin"] for c in self.service.list_chats("user-one", "agent-one")}, {"web", "telegram"})
        self.assertIsNone(self.service.latest_chat("user-one", origin="other"))

    def test_fresh_session_ignores_a_recent_web_chat_and_starts_a_new_chat(self) -> None:
        tele = self._chat("telegram", timedelta(hours=2))
        web = self._chat("web", timedelta(minutes=5))
        session = TelegramSession("user-one", "agent-one")
        _prepare_message_session(session, self.service)
        self.assertNotIn(session.chat_id, (web, tele))
        self.assertEqual(self.source.origin(session.chat_id), "telegram")
        self.assertEqual(session.pending_reroute.previous_chat_id, tele)

    def test_fresh_session_resumes_a_recent_telegram_chat_silently(self) -> None:
        tele = self._chat("telegram", timedelta(minutes=10))
        self._chat("web", timedelta(minutes=1))
        session = TelegramSession("user-one", "agent-one")
        _prepare_message_session(session, self.service)
        self.assertEqual(session.chat_id, tele)
        self.assertIsNone(session.pending_reroute)

    def test_session_that_knows_its_chat_is_unchanged(self) -> None:
        tele = self._chat("telegram", timedelta(minutes=10))
        self._chat("web", timedelta(minutes=1))
        session = TelegramSession("user-one", "agent-one", tele)
        session.last_message_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        _prepare_message_session(session, self.service)
        self.assertEqual(session.chat_id, tele)
        self.assertIsNone(session.pending_reroute)


if __name__ == "__main__":
    unittest.main()
