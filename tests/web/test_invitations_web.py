"""Invitation, leave and unshare routes."""

from __future__ import annotations

import unittest
from unittest.mock import patch

from src.service.invitations import InvitationStore
from tests.web.test_shared_agents_web import SHARED, SPACE, SharedAgentFixture


class InvitationRouteTests(SharedAgentFixture):
    def setUp(self) -> None:
        super().setUp()
        for target in ("src.admin.spaces.CONFIG_DIR", "src.admin.spaces.DATA_DIR", "src.service.chat.DATA_DIR"):
            patcher = patch(target, self.config if "CONFIG" in target else self.root)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.service._invitations = InvitationStore(self.root)
        (self.config / SPACE / "agents").mkdir(exist_ok=True)
        (self.config / SPACE / "agents" / f"{SHARED}.yaml").write_text(f"id: {SHARED}\nname: Nova\n", encoding="utf-8")
        patcher = patch.object(type(self.service), "reindex", lambda s, u, a, full=False: {})
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_invite_list_and_accept(self) -> None:
        one, three = self._client_as("user-one"), self._client_as("user-three")
        created = one.post(f"/api/agents/{SHARED}/invitations", json={"user_id": "user-three"})
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(set(created.json()), {"id", "agent_id", "invitee", "status", "created"})
        incoming = three.get("/api/invitations").json()
        self.assertEqual(incoming["pending_count"], 1)
        self.assertEqual(set(incoming["incoming"][0]),
                         {"id", "agent_id", "agent_name", "inviter", "inviter_name", "billing_user", "created"})
        self.assertEqual(one.get("/api/invitations").json()["outgoing"][0]["invitee"], "user-three")
        done = three.post(f"/api/invitations/{created.json()['id']}/accept")
        self.assertEqual(done.status_code, 200, done.text)
        self.assertNotEqual(done.json()["space_id"], SPACE)
        self.assertEqual(three.get("/api/invitations").json()["pending_count"], 0)

    def test_non_member_gets_404_and_decline_revoke(self) -> None:
        three = self._client_as("user-three")
        self.assertEqual(three.post(f"/api/agents/{SHARED}/invitations", json={"user_id": "user-two"}).status_code, 404)
        one = self._client_as("user-one")
        first = one.post(f"/api/agents/{SHARED}/invitations", json={"user_id": "user-three"}).json()["id"]
        self.assertEqual(three.post(f"/api/invitations/{first}/decline").status_code, 204)
        second = one.post(f"/api/agents/{SHARED}/invitations", json={"user_id": "user-three"}).json()["id"]
        self.assertEqual(three.post(f"/api/invitations/{second}/revoke").status_code, 404)
        self.assertEqual(one.post(f"/api/invitations/{second}/revoke").status_code, 204)
        self.assertEqual(one.post(f"/api/invitations/{second}/accept").status_code, 404)

    def test_leave_and_unshare(self) -> None:
        one = self._client_as("user-one")
        left = one.post(f"/api/agents/{SHARED}/leave")
        self.assertEqual(left.status_code, 200, left.text)
        self.assertEqual(self._client_as("user-three").post(f"/api/agents/{SHARED}/leave").status_code, 404)
        two = self._client_as("user-two")
        self.assertEqual(two.post(f"/api/agents/{SHARED}/leave").status_code, 400)  # alone: unshare instead
        done = two.post(f"/api/agents/{SHARED}/unshare")
        self.assertEqual(done.status_code, 200, done.text)
        self.assertEqual(done.json()["agents"], {"user-two": SHARED})


if __name__ == "__main__":
    unittest.main()
