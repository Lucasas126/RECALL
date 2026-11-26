from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src.agent.agent import Agent
from src.core.sources.conversations import ConversationsSource
from tests.tasks import test_service_steering as steering

TS = "2026-11-13T10:07:12+01:00"


def _users(user_id: str):
    return SimpleNamespace(name={"user-a": "Ana", "user-b": "Ben"}[user_id])


class AuthorPrefixTests(unittest.TestCase):
    def test_prefix_is_the_upper_cased_name(self) -> None:
        with patch("src.agent.agent.load_user", _users):
            self.assertEqual(Agent._stamped("hi", TS, "web", "user-a"), "[ANA] [2026-11-13 10:07+01:00 via web UI] hi")
            parts = Agent._stamped([{"type": "image_url", "image_url": {"url": "x"}}], TS, None, "user-b")
        self.assertEqual(parts[0]["text"], "[BEN] [2026-11-13 10:07+01:00]")

    def test_unknown_author_falls_back_to_the_id(self) -> None:
        self.assertEqual(Agent._stamped("hi", TS, None, "gone"), "[gone] [2026-11-13 10:07+01:00] hi")

    def test_no_author_is_unchanged(self) -> None:
        self.assertEqual(Agent._stamped("hi", TS, "web"), "[2026-11-13 10:07+01:00 via web UI] hi")


class AuthorStorageTests(steering.SteeringFixture):
    def _users_in_context(self) -> list[str]:
        # the fixture strips prefixes from the provider snapshot: read the agent's own context
        return [m["content"] for m in self.agents[("user-one", "agent-one")].messages if m["role"] == "user"]

    def test_author_is_stored_returned_and_prefixed_on_replay(self) -> None:
        with patch("src.agent.agent.load_user", _users):
            list(self.service.send("user-one", "agent-one", self.chat, "First", author="user-a"))
            list(self.service.send("user-one", "agent-one", self.chat, "Second", author="user-b"))
            messages = self.service.get_chat("user-one", "agent-one", self.chat)["messages"]
            self.assertEqual([m.get("author") for m in messages if m["role"] == "user"], ["user-a", "user-b"])
            stored = self.sources[("user-one", "agent-one")].load(self.chat)
            self.assertEqual([t.get("author") for t in stored if t["role"] == "user"], ["user-a", "user-b"])
            self.assertTrue(self._users_in_context()[-1].startswith("[BEN] "))
            # a fresh agent replays the history: the old turn keeps its prefix
            self.service._slot("user-one", "agent-one").agents.clear()
            list(self.service.send("user-one", "agent-one", self.chat, "Third", author="user-a"))
            sent = self._users_in_context()
            self.assertTrue(sent[0].startswith("[ANA] ") and sent[1].startswith("[BEN] ") and sent[2].startswith("[ANA] "))

    def test_get_chat_returns_the_author_name(self) -> None:
        with patch("src.agent.agent.load_user", _users):
            list(self.service.send("user-one", "agent-one", self.chat, "First", author="user-a"))
            list(self.service.send("user-one", "agent-one", self.chat, "Second", author="user-a"))
        real = self.service._user
        with patch.object(self.service, "_user", side_effect=lambda uid: _users(uid) if uid == "user-a" else real(uid)) as lookup:
            users = [m for m in self.service.get_chat("user-one", "agent-one", self.chat)["messages"] if m["role"] == "user"]
        self.assertEqual([m["author_name"] for m in users], ["Ana", "Ana"])
        self.assertEqual([c.args[0] for c in lookup.call_args_list].count("user-a"), 1)  # resolved once per call

    def test_private_chat_has_no_author_name(self) -> None:
        list(self._send("Hello"))
        messages = self.service.get_chat("user-one", "agent-one", self.chat)["messages"]
        self.assertTrue(all("author_name" not in m for m in messages))

    def test_rewind_keeps_the_author(self) -> None:
        with patch("src.agent.agent.load_user", _users):
            list(self.service.send("user-one", "agent-one", self.chat, "First", author="user-b"))
            list(self.service.send("user-one", "agent-one", self.chat, "First, fixed", rewind_to=0))
            stored = self.sources[("user-one", "agent-one")].load(self.chat)
            self.assertEqual(stored[0]["content"], "First, fixed")
            self.assertEqual(stored[0].get("author"), "user-b")
            self.assertTrue(self._users_in_context()[0].startswith("[BEN] "))

    def test_private_agent_has_no_author_and_no_prefix(self) -> None:
        list(self._send("Hello"))
        stored = self.sources[("user-one", "agent-one")].load(self.chat)
        self.assertNotIn("author", stored[0])
        self.assertTrue(self._users_in_context()[0].startswith("[20"))


if __name__ == "__main__":
    unittest.main()
