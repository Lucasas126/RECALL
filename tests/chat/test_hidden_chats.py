"""Hidden chats: the marker, the listings, @mention chats born hidden, and indexing/search left as they were."""

from __future__ import annotations

import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from src.core.sources.registry import SourceRegistry
from tests.tasks import test_service_steering as steering

A = ("user-one", "agent-one")
B = ("user-one", "agent-two")


class HiddenMarkerTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.source = SourceRegistry.for_agent("u", "a", data_dir=Path(directory.name)).conversations()

    def test_marker_is_a_sidecar_set_cleared_and_in_the_summary(self) -> None:
        chat = self.source.create()
        self.assertFalse(self.source.is_hidden(chat))
        self.assertFalse(self.source.summary(chat)["hidden"])
        self.source.set_hidden(chat, True)
        self.assertTrue((self.source.root / f"{chat}.hidden.json").is_file())
        self.assertTrue(self.source.is_hidden(chat))
        self.assertEqual(self.source.hidden_chats(), {chat})
        self.assertTrue(self.source.summary(chat)["hidden"])
        self.assertEqual(self.source.chats(), [chat], "a hidden chat is still a chat of the source")
        self.source.set_hidden(chat, False)
        self.assertFalse(self.source.is_hidden(chat))

    def test_create_hidden_and_delete_removes_the_marker(self) -> None:
        chat = self.source.create(hidden=True)
        self.assertTrue(self.source.is_hidden(chat))
        self.source.delete_chat(chat)
        self.assertFalse(any(self.source.root.glob(f"{chat}*")))

    def test_unknown_chat_cannot_be_marked(self) -> None:
        with self.assertRaises(FileNotFoundError):
            self.source.set_hidden("20240101-000000-abcdef", True)

    def test_hidden_chat_stays_searchable_and_indexable(self) -> None:
        chat = self.source.create(hidden=True)
        self.source.append(chat, "user", "needle in a hidden chat")
        self.assertIn(f"{chat}.jsonl", self.source.walk({"jsonl"}))
        self.assertNotIn(chat, self.source.disposable_chats() | self.source.task_chats())
        self.assertIn(chat, self.source.chats(include_internal=True))


class HiddenListingTests(steering.SteeringFixture):
    def _mention_chat(self) -> str:
        list(self._send("@agent-two hello"))
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            copied = [t for t in self.sources[A].load(self.chat) if t.get("mention_reply")]
            if copied:
                return copied[-1]["mention_reply"]["chat_id"]
            time.sleep(0.02)
        self.fail("the mention reply was not copied")

    def test_mention_chat_is_born_hidden_and_a_direct_chat_is_not(self) -> None:
        mention = self._mention_chat()
        self.assertTrue(self.sources[B].is_hidden(mention))
        direct = self.service.create_chat("user-one", "agent-two")["id"]
        self.assertFalse(self.sources[B].is_hidden(direct))
        self.assertFalse(self.service.create_chat("user-one", "agent-one")["hidden"])

    def test_listings_exclude_hidden_unless_asked_and_mark_them(self) -> None:
        mention = self._mention_chat()
        direct = self.service.create_chat("user-one", "agent-two")["id"]
        self.sources[B].append(direct, "user", "hi")
        plain = [c["id"] for c in self.service.list_chats("user-one", "agent-two", keep=direct)]
        self.assertEqual(plain, [direct])
        every = {c["id"]: c for c in self.service.list_chats("user-one", "agent-two", keep=direct, include_hidden=True)}
        self.assertEqual(set(every), {direct, mention})
        self.assertTrue(every[mention]["hidden"])
        self.assertFalse(every[direct]["hidden"])
        self.assertNotIn(mention, [c["id"] for c in self.service.list_all_chats("user-one")])
        self.assertIn(mention, [c["id"] for c in self.service.list_all_chats("user-one", include_hidden=True)])

    def test_a_message_sent_directly_into_a_hidden_chat_keeps_it_hidden_and_reachable(self) -> None:
        mention = self._mention_chat()
        list(self.service.send("user-one", "agent-two", mention, "direct follow-up", stream=True))
        self.assertTrue(self.sources[B].is_hidden(mention))
        self.assertIn(mention, [c["id"] for c in self.service.list_chats("user-one", "agent-two", include_hidden=True)])
        self.service.get_chat("user-one", "agent-two", mention)  # reachable by id

    def test_latest_chat_still_counts_hidden_chats(self) -> None:
        mention = self._mention_chat()
        self.sources[B].set_hidden(mention, True)
        latest = self.service.latest_chat("user-one", origin="mention")
        self.assertEqual(latest["id"], mention)


if __name__ == "__main__":
    unittest.main()
