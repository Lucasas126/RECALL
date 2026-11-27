from __future__ import annotations

import unittest

from src.config.models import ProviderConfig, UserConfig
from tests.tasks import test_service_steering as steering


class PersonMentionTests(steering.SteeringFixture):
    SHARED = False
    AGENTS = ["agent-one", "agent-two"]

    def _user(self, user_id: str) -> UserConfig:
        if self.SHARED:
            return UserConfig(
                id="space-0a1b2c3d", name="Team", kind="space", members=["user-a", "user-b", "nova"],
                billing_user="user-a", agents=self.AGENTS, provider=ProviderConfig(name="ollama"),
            )
        return super()._user(user_id)

    def _calls(self) -> int:
        return sum(len(c) for c in self.calls.values())

    def _stored(self) -> list[dict]:
        return self.sources[("user-one", "agent-one")].load(self.chat)

    def _say(self, text: str, author: str = "user-a") -> list:
        return list(self.service.send("user-one", "agent-one", self.chat, text, author=author))

    def test_member_mention_is_stored_without_running_the_agent(self) -> None:
        self.SHARED = True
        events = self._say("@user-b lunch at noon?")
        self.assertEqual([e.kind for e in events], ["delivered"])
        self.assertEqual(events[0].data, {"to": "user-b"})
        self.assertEqual(self._calls(), 0)
        stored = self._stored()
        self.assertEqual([(t["role"], t.get("author")) for t in stored], [("user", "user-a")])
        self.assertEqual(stored[0]["content"], "@user-b lunch at noon?")

    def test_agent_wins_when_the_id_is_also_a_member(self) -> None:
        self.SHARED = True
        self.AGENTS = ["agent-one", "agent-two", "nova"]
        self.assertIsNone(self.service._person_mention("user-one", "agent-one", "user-a", "@nova hi"))

    def test_mentioning_yourself_or_an_unknown_id_is_a_normal_message(self) -> None:
        self.SHARED = True
        self.assertIsNone(self.service._person_mention("user-one", "agent-one", "user-a", "@user-a hi"))
        self.assertIsNone(self.service._person_mention("user-one", "agent-one", "user-a", "@nobody hi"))
        self.assertEqual(self.service._person_mention("user-one", "agent-one", "user-a", "@user-b hi"), "user-b")

    def test_private_agent_mention_is_a_normal_message(self) -> None:
        list(self.service.send("user-one", "agent-one", self.chat, "@user-b hello"))
        self.assertEqual([t["role"] for t in self._stored()], ["user", "assistant"])
        self.assertGreater(self._calls(), 0)

    def test_agent_mention_still_routes_to_the_agent(self) -> None:
        self.SHARED = True
        self.assertEqual(self.service._mention_target("user-one", "agent-one", "@agent-two do it"), ("agent-two", "do it"))
        self.assertIsNone(self.service._person_mention("user-one", "agent-one", "user-a", "@agent-two do it"))

    def test_mention_anywhere_and_several_people_make_one_people_message(self) -> None:
        self.SHARED = True
        events = self._say("hey @user-b, and also @nova: ping")
        self.assertEqual([(e.kind, e.data) for e in events], [("delivered", {"to": "user-b"})])
        self.assertEqual(self._calls(), 0)
        self.assertEqual(len(self._stored()), 1)

    def test_person_then_agent_is_a_people_message(self) -> None:
        self.SHARED = True
        self.assertEqual(self.service._person_mention("user-one", "agent-one", "user-a", "@user-b ask @agent-two"), "user-b")
        self.assertIsNone(self.service._mention_target("user-one", "agent-one", "@user-b ask @agent-two", "user-a"))
        events = self._say("@user-b ask @agent-two")
        self.assertEqual([e.kind for e in events], ["delivered"])
        self.assertEqual(self._calls(), 0)

    def test_agent_then_person_routes_to_the_agent_and_the_person_is_plain_text(self) -> None:
        self.SHARED = True
        self.assertEqual(
            self.service._mention_target("user-one", "agent-one", "@agent-two tell @user-b", "user-a"),
            ("agent-two", "tell @user-b"),
        )
        self.assertIsNone(self.service._person_mention("user-one", "agent-one", "user-a", "@agent-two tell @user-b"))
        self.assertEqual(
            self.service._mention_target("user-one", "agent-one", "hi @agent-two, tell @user-b", "user-a"),
            ("agent-two", "hi @agent-two, tell @user-b"),
        )

    def test_two_agents_only_the_first_acts(self) -> None:
        self.SHARED = True
        self.AGENTS = ["agent-one", "agent-two", "agent-three"]
        self.assertEqual(
            self.service._mention_target("user-one", "agent-one", "@agent-three then @agent-two"),
            ("agent-three", "then @agent-two"),
        )
        self.assertEqual(
            self.service._mention_target("user-one", "agent-one", "x @agent-two and @agent-three")[0], "agent-two",
        )

    def test_unknown_first_mention_is_skipped(self) -> None:
        self.SHARED = True
        self.assertEqual(self.service._person_mention("user-one", "agent-one", "user-a", "@nobody @user-b hi"), "user-b")

    def test_private_agent_people_ids_are_plain_text(self) -> None:
        self.assertIsNone(self.service._person_mention("user-one", "agent-one", "user-a", "hello @user-b"))

    def test_later_reply_sees_the_turn_with_the_author_prefix(self) -> None:
        self.SHARED = True
        self._say("@user-b hello there")
        self.SHARED = False  # the fixture has no config folder for a space: the later turn runs as in the other tests
        self._say("question for the agent", author="user-b")
        sent = [m["content"] for m in self.agents[("user-one", "agent-one")].messages if m["role"] == "user"]
        self.assertEqual(len(sent), 2)
        self.assertIn("hello there", sent[0])


if __name__ == "__main__":
    unittest.main()
