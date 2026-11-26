"""Invitations to share an agent: invite, accept, decline, revoke, leave."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from src.config.spaces import all_spaces, clear_spaces_cache
from src.service.agent_move import MoveError
from src.service.chat import InvitationError, UnknownAgent, UnknownInvitation
from tests.support.admin_helpers import AdminFixture


class InvitationTests(AdminFixture):
    def setUp(self) -> None:
        super().setUp()
        clear_spaces_cache()
        for target, value in (("src.admin.spaces.CONFIG_DIR", self.config_dir), ("src.admin.spaces.DATA_DIR", self.data_dir)):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        for user in ("user-b", "user-c"):
            (self.config_dir / user / "agents").mkdir(parents=True)
            (self.config_dir / user / "user.yaml").write_text(
                f"id: {user}\nname: {user.upper()}\nmax_level: admin\nprovider: {{name: ollama, api_key: K_{user[-1]}}}\nagents: []\n",
                encoding="utf-8")
        patcher = patch.object(type(self.service), "reindex", lambda s, u, a, full=False: {})
        patcher.start()
        self.addCleanup(patcher.stop)

    def _owner(self, person: str, agent: str = "bot") -> str:
        return self.service.agent_owner(person, agent)

    def test_invite_accept_moves_the_agent_into_a_space(self) -> None:
        record = self.service.invite("user-a", "bot", "user-b")
        listing = self.service.list_invitations("user-b")
        self.assertEqual(listing["pending_count"], 1)
        self.assertEqual(listing["incoming"][0]["id"], record["id"])
        self.assertEqual(listing["incoming"][0]["billing_user"], "user-a")
        self.assertEqual(self.service.list_invitations("user-a")["outgoing"][0]["invitee"], "user-b")
        self.assertEqual(self._owner("user-a"), "user-a")  # still private
        result = self.service.accept("user-b", record["id"])
        self.assertEqual(self._owner("user-a"), result["space_id"])
        self.assertEqual(self._owner("user-b"), result["space_id"])
        self.assertEqual(self.service.list_invitations("user-b")["pending_count"], 0)

    def test_second_invitation_enlarges_the_group(self) -> None:
        first = self.service.invite("user-a", "bot", "user-b")
        second = self.service.invite("user-a", "bot", "user-c")
        space = self.service.accept("user-b", first["id"])["space_id"]
        self.assertEqual(self.service.list_invitations("user-a")["outgoing"][0]["invitee"], "user-c")
        newer = self.service.accept("user-c", second["id"])["space_id"]
        self.assertNotEqual(space, newer)
        self.assertEqual([set(s.members) for s in all_spaces(self.config_dir)], [{"user-a", "user-b", "user-c"}])
        self.assertEqual(self._owner("user-c"), newer)

    def test_a_member_invites_into_a_shared_agent(self) -> None:
        space = self.service.accept("user-b", self.service.invite("user-a", "bot", "user-b")["id"])["space_id"]
        record = self.service.invite("user-b", "bot", "user-c")
        self.assertEqual(record["owner"], space)
        self.service.accept("user-c", record["id"])
        self.assertEqual(self._owner("user-c"), self._owner("user-a"))

    def test_decline_and_revoke(self) -> None:
        declined = self.service.invite("user-a", "bot", "user-b")
        self.service.decline("user-b", declined["id"])
        self.assertEqual(self.service.list_invitations("user-b")["pending_count"], 0)
        with self.assertRaises(UnknownInvitation):
            self.service.accept("user-b", declined["id"])
        revoked = self.service.invite("user-a", "bot", "user-b")
        with self.assertRaises(UnknownInvitation):
            self.service.revoke("user-c", revoked["id"])
        self.service.revoke("user-a", revoked["id"])
        self.assertEqual(self.service.list_invitations("user-a")["outgoing"], [])
        with self.assertRaises(UnknownInvitation):
            self.service.accept("user-b", revoked["id"])

    def test_refusals(self) -> None:
        with self.assertRaises(UnknownAgent):
            self.service.invite("user-b", "bot", "user-c")  # not a member
        with self.assertRaises(InvitationError):
            self.service.invite("user-a", "bot", "user-a")
        with self.assertRaises(InvitationError):
            self.service.invite("user-a", "bot", "nobody")
        self.service.invite("user-a", "bot", "user-b")
        with self.assertRaises(InvitationError):
            self.service.invite("user-a", "bot", "user-b")  # duplicate

    def test_accept_by_someone_else_and_id_clash_at_accept(self) -> None:
        record = self.service.invite("user-a", "bot", "user-b")
        with self.assertRaises(UnknownInvitation):
            self.service.accept("user-c", record["id"])
        with patch("src.service.agent_move.agent_id_conflict", return_value="user-b"):
            with self.assertRaises(MoveError):
                self.service.accept("user-b", record["id"])
        self.assertEqual(self.service.list_invitations("user-b")["pending_count"], 1)  # still pending

    def test_leave_moves_to_the_remaining_members(self) -> None:
        for user in ("user-b", "user-c"):
            self.service.accept(user, self.service.invite("user-a", "bot", user)["id"])
        target = self.service.leave_shared("user-c", "bot")
        self.assertEqual(self._owner("user-a"), target)
        self.assertEqual(self._owner("user-b"), target)
        self.assertEqual(self.service.agent_owner("user-c", "bot"), "user-c")
        self.assertEqual([set(s.members) for s in all_spaces(self.config_dir)], [{"user-a", "user-b"}])
        again = self.service.leave_shared("user-b", "bot")  # a single-member space is allowed
        self.assertEqual([set(s.members) for s in all_spaces(self.config_dir)], [{"user-a"}])
        self.assertEqual(self._owner("user-a"), again)
        with self.assertRaises(InvitationError):
            self.service.leave_shared("user-a", "bot")


if __name__ == "__main__":
    unittest.main()
