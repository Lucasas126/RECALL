"""Shared agents over the web API: a space's agent for its members, 404 for others, fail closed, no spaces in user lists."""

from __future__ import annotations

import os
import shutil
import unittest
from unittest.mock import patch

os.environ.setdefault("RECALL_SESSION_SECRET", "x")

from fastapi.testclient import TestClient

from src.config import spaces
from src.config.loader import load_user as real_load_user
from src.web.app import create_app
from tests.support.auth_helpers import TEST_PASSWORD_HASH, login
from tests.tasks import test_service_steering as steering

SPACE = "space-0a1b2c3d"
SHARED = "nova"


def _user_yaml(user_id: str, level: str = "admin") -> str:
    return (f"id: {user_id}\nname: {user_id}\nmax_level: {level}\nprovider: {{name: ollama}}\n"
            f"agents: [agent-one]\npassword_hash: '{TEST_PASSWORD_HASH}'\n")


def _space_yaml(members: list[str]) -> str:
    return (f"id: {SPACE}\nname: Shared\nkind: space\nmembers: [{', '.join(members)}]\nbilling_user: {members[0]}\n"
            f"provider: {{name: ollama}}\nagents: [{SHARED}]\n")


class SharedAgentFixture(steering.SteeringFixture):
    """user-one and user-two share a space with the agent `nova`; user-three is not a member."""

    def setUp(self) -> None:
        super().setUp()
        self.config = self.root / "config"
        for user in ("user-one", "user-two", "user-three"):
            (self.config / user).mkdir(exist_ok=True)
            (self.config / user / "user.yaml").write_text(_user_yaml(user), encoding="utf-8")
        self._write_space(["user-one", "user-two"])
        for user in ("user-one", "user-two", "user-three"):
            self._add_agent(user, "agent-one")
        self._add_agent(SPACE, SHARED)
        spaces.clear_spaces_cache()
        self.addCleanup(spaces.clear_spaces_cache)
        for target in ("src.service.chat.CONFIG_DIR", "src.config.loader.CONFIG_DIR"):
            patcher = patch(target, self.config)
            patcher.start()
            self.addCleanup(patcher.stop)
        loader = patch("src.service.chat.load_user", side_effect=real_load_user)
        loader.start()
        self.addCleanup(loader.stop)
        self.client = TestClient(create_app(self.service))
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)

    def _write_space(self, members: list[str]) -> None:
        (self.config / SPACE).mkdir(exist_ok=True)
        (self.config / SPACE / "user.yaml").write_text(_space_yaml(members), encoding="utf-8")

    def _add_agent(self, owner: str, agent: str) -> None:
        key = (owner, agent)
        if key in self.sources:
            return
        from src.core.sources.conversations import ConversationsSource
        from unittest.mock import Mock
        from src.providers.provider import LLMProvider
        root = self.root / owner / agent
        root.mkdir(parents=True)
        self.sources[key] = ConversationsSource(root)
        self.calls[key] = []
        provider = Mock(spec=LLMProvider)
        provider.context_window.return_value = 10000
        provider.invoke.side_effect = self._responder(key)
        self.providers[key] = provider

    def _client_as(self, user: str) -> TestClient:
        client = TestClient(create_app(self.service))
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        login(client, user)
        return client


class SharedAgentMemberTests(SharedAgentFixture):
    def test_a_member_lists_the_space_agent_marked_shared(self) -> None:
        client = self._client_as("user-one")
        agents = {a["id"]: a for a in client.get("/api/agents").json()}
        self.assertTrue(agents[SHARED]["shared"])
        self.assertEqual(agents[SHARED]["space_id"], SPACE)
        self.assertNotIn("shared", agents["agent-one"])

    def test_the_shared_agent_lists_its_members_with_their_names(self) -> None:
        client = self._client_as("user-one")
        for items in (client.get("/api/agents").json(), client.get("/api/me").json()["agents"]):
            shared = next(a for a in items if a["id"] == SHARED)
            self.assertIn("user-one", [m["id"] for m in shared["members"]])
            self.assertTrue(all(m["name"] for m in shared["members"]))
            self.assertNotIn("members", next(a for a in items if a["id"] == "agent-one"))

    def test_a_member_opens_chats_sends_and_reads(self) -> None:
        client = self._client_as("user-one")
        chat = client.post(f"/api/agents/{SHARED}/chats").json()["id"]
        self.assertEqual(client.get(f"/api/agents/{SHARED}/chats").status_code, 200)
        sent = client.post(f"/api/agents/{SHARED}/chats/{chat}/messages", json={"content": "hello"})
        self.assertEqual(sent.status_code, 200, sent.text)
        self.assertRegex(sent.text, r"Answer: .*hello")  # the stamped user text may carry a prefix
        contents = [m["content"] for m in client.get(f"/api/agents/{SHARED}/chats/{chat}").json()["messages"]]
        self.assertTrue(any(c.startswith("Answer: ") and c.endswith("hello") for c in contents), contents)
        self.assertEqual(client.get(f"/api/agents/{SHARED}/chats/{chat}/approvals").json(), [])

    def test_a_member_sees_the_pending_approvals_of_the_shared_chat(self) -> None:
        client = self._client_as("user-one")
        chat = client.post(f"/api/agents/{SHARED}/chats").json()["id"]
        with patch.object(self.service._approvals, "pending", return_value=[{"id": "a"}]) as pending:
            listed = client.get(f"/api/agents/{SHARED}/chats/{chat}/approvals").json()
        self.assertEqual(listed, [{"id": "a"}])
        self.assertEqual(pending.call_args.args[:2], (SPACE, SHARED))  # keyed by the space, not by the person

    def test_a_second_member_sees_the_same_chat_in_the_chat_list(self) -> None:
        first, second = self._client_as("user-one"), self._client_as("user-two")
        chat = first.post(f"/api/agents/{SHARED}/chats").json()["id"]
        first.post(f"/api/agents/{SHARED}/chats/{chat}/messages", json={"content": "hi"})
        seen = [c for c in second.get("/api/chats").json() if c["id"] == chat]
        self.assertEqual(len(seen), 1)
        self.assertEqual((seen[0]["agent_id"], seen[0]["shared"], seen[0]["space_id"]), (SHARED, True, SPACE))
        self.assertEqual(second.get(f"/api/agents/{SHARED}/chats/{chat}").status_code, 200)


class SharedAgentAccessTests(SharedAgentFixture):
    def test_a_non_member_gets_404_on_every_kind_of_space_agent_route(self) -> None:
        member = self._client_as("user-one")
        chat = member.post(f"/api/agents/{SHARED}/chats").json()["id"]
        outsider = self._client_as("user-three")
        base = f"/api/agents/{SHARED}"
        for method, path in (
            ("get", base), ("get", f"{base}/chats"), ("post", f"{base}/chats"), ("get", f"{base}/chats/{chat}"),
            ("get", f"{base}/chats/{chat}/approvals"), ("get", f"{base}/soul"), ("get", f"{base}/approval-rules"),
        ):
            self.assertEqual(getattr(outsider, method)(path).status_code, 404, f"{method} {path}")
        sent = outsider.post(f"{base}/chats/{chat}/messages", json={"content": "x"})
        self.assertEqual(sent.status_code, 404)

    def test_a_non_member_does_not_see_the_space_agent_or_chats(self) -> None:
        member = self._client_as("user-one")
        member.post(f"/api/agents/{SHARED}/chats")
        outsider = self._client_as("user-three")
        self.assertNotIn(SHARED, [a["id"] for a in outsider.get("/api/agents").json()])
        self.assertEqual([c for c in outsider.get("/api/chats").json() if c.get("space_id")], [])

    def test_a_space_cannot_log_in(self) -> None:
        response = TestClient(create_app(self.service)).post("/api/login", json={"user_id": SPACE, "password": "x"})
        self.assertEqual(response.status_code, 401)

    def test_removing_a_member_closes_the_agent_to_them(self) -> None:
        client = self._client_as("user-two")
        self.assertEqual(client.get(f"/api/agents/{SHARED}/chats").status_code, 200)
        self._write_space(["user-one"])
        self.assertEqual(client.get(f"/api/agents/{SHARED}/chats").status_code, 404)


class SharedAgentFailClosedTests(SharedAgentFixture):
    def test_a_space_with_a_missing_member_is_unusable_and_skipped(self) -> None:
        client = self._client_as("user-one")
        self.assertEqual(client.get(f"/api/agents/{SHARED}/chats").status_code, 200)
        self._write_space(["user-one", "user-gone"])
        self.assertEqual(client.get(f"/api/agents/{SHARED}/chats").status_code, 404)
        self.assertEqual(client.post(f"/api/agents/{SHARED}/chats").status_code, 404)
        self.assertNotIn(SHARED, [a["id"] for a in client.get("/api/agents").json()])
        self.assertNotIn(SHARED, [a["id"] for a in client.get("/api/agents").json()])
        self.assertEqual([c for c in client.get("/api/chats").json() if c.get("space_id")], [])
        self.assertTrue(all("space_id" not in a for a in client.get("/api/agents").json()))
        self.assertEqual(client.get("/api/agents").status_code, 200)  # the person's own agents still work

    def test_a_member_who_became_a_space_or_invalid_user_fails_closed(self) -> None:
        client = self._client_as("user-one")
        (self.config / "user-two" / "user.yaml").write_text("id: user-two\nname: [broken\n", encoding="utf-8")
        self.assertEqual(client.get(f"/api/agents/{SHARED}/chats").status_code, 404)


class SpacesNotListedAsUsersTests(SharedAgentFixture):
    def test_user_overview_and_the_admin_list_leave_spaces_out(self) -> None:
        ids = [row["id"] for row in self.service.user_overview()]
        self.assertEqual(ids, ["user-one", "user-three", "user-two"])
        listed = [row["id"] for row in self._client_as("user-one").get("/api/admin/users").json()]
        self.assertNotIn(SPACE, listed)
        self.assertIn("user-two", listed)


class SpacesCacheTests(SharedAgentFixture):
    def test_a_changed_user_yaml_invalidates_the_cache(self) -> None:
        self.assertEqual(spaces.spaces_of("user-two", self.config), [SPACE])
        self._write_space(["user-one"])
        self.assertEqual(spaces.spaces_of("user-two", self.config), [])
        self.assertEqual(spaces.spaces_of("user-one", self.config), [SPACE])

    def test_an_added_and_a_removed_space_folder_invalidate_the_cache(self) -> None:
        other = "space-ffffffff"
        self.assertEqual([s.id for s in spaces.all_spaces(self.config)], [SPACE])
        (self.config / other).mkdir()
        (self.config / other / "user.yaml").write_text(_space_yaml(["user-one"]).replace(SPACE, other), encoding="utf-8")
        self.assertEqual([s.id for s in spaces.all_spaces(self.config)], [SPACE, other])
        shutil.rmtree(self.config / SPACE)
        self.assertEqual([s.id for s in spaces.all_spaces(self.config)], [other])

    def test_an_unchanged_space_is_not_parsed_again(self) -> None:
        spaces.all_spaces(self.config)
        with patch.object(spaces, "_read", side_effect=AssertionError("parsed again")):
            self.assertEqual([s.id for s in spaces.all_spaces(self.config)], [SPACE])

    def test_the_cache_hands_out_copies(self) -> None:
        spaces.all_spaces(self.config)[0].members.append("user-three")
        self.assertEqual(spaces.spaces_of("user-three", self.config), [])


if __name__ == "__main__":
    unittest.main()
