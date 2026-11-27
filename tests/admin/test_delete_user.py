"""Deleting a user: service rules, shared agents, invitations, leftovers, CLI and web route."""

from __future__ import annotations

import contextlib
import io
import json
from unittest.mock import patch

from src.admin import delete_user as delete_module
from src.config.spaces import all_spaces, clear_spaces_cache
from src.service.chat import AgentBusy, InputError, NotAdministrator, UnknownManagedUser
from tests.support.admin_helpers import AdminFixture


class DeleteUserTests(AdminFixture):
    def setUp(self) -> None:
        super().setUp()
        clear_spaces_cache()
        for target, value in (("src.admin.spaces.CONFIG_DIR", self.config_dir), ("src.admin.spaces.DATA_DIR", self.data_dir),
                              ("src.admin.delete_user.CONFIG_DIR", self.config_dir), ("src.admin.delete_user.DATA_DIR", self.data_dir)):
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        patcher = patch.object(type(self.service), "reindex", lambda *a, **k: {})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.add_user("user-b", "collaborator", "B_KEY")
        self.add_user("user-c", "admin", "C_KEY", "[]")
        (self.data_dir / "user-b" / "nova").mkdir(parents=True)
        (self.data_dir / "user-b" / "nova" / "note.txt").write_text("x", encoding="utf-8")

    def add_user(self, user_id: str, level: str, key: str, agents: str = "[nova]") -> None:
        folder = self.config_dir / user_id
        (folder / "agents").mkdir(parents=True)
        (folder / "user.yaml").write_text(
            f"id: {user_id}\nname: X\nmax_level: {level}\nprovider: {{name: ollama, api_key: {key}}}\nagents: {agents}\n", encoding="utf-8")
        (folder / "agents" / "nova.yaml").write_text("id: nova\nname: Nova\n", encoding="utf-8")

    def delete(self, user="user-b", **kw):
        return self.service.delete_user(kw.pop("admin", "user-a"), user, confirm=kw.pop("confirm", user))

    def test_removes_config_and_data_and_lists_leftovers_by_name(self) -> None:
        with patch("src.admin.delete_user.get_secret", return_value="1=user-b,2=user-a"):
            result = self.delete()
        self.assertFalse((self.config_dir / "user-b").exists())
        self.assertFalse((self.data_dir / "user-b").exists())
        self.assertEqual(result["leftovers"]["secrets"], ["B_KEY"])
        self.assertTrue(result["leftovers"]["telegram"])
        self.assertNotIn("1=user-b", json.dumps(result))
        audit = (self.data_dir / "user-a" / "admin_audit.jsonl").read_text(encoding="utf-8")
        self.assertIn("delete_user", audit)

    def test_refusals_leave_everything(self) -> None:
        for kwargs, error in (({"admin": "user-b", "user": "user-b"}, NotAdministrator), ({"confirm": "nope"}, InputError),
                              ({"admin": "user-a", "user": "user-a"}, InputError), ({"user": "ghost"}, UnknownManagedUser)):
            with self.assertRaises(error):
                self.delete(**kwargs)
        self.assertTrue((self.config_dir / "user-b").exists())
        (self.config_dir / "space-0a1b2c3d").mkdir()
        with self.assertRaises(InputError):
            self.delete("space-0a1b2c3d")

    def test_last_administrator_is_refused(self) -> None:
        self.delete("user-c")
        with self.assertRaises(InputError):
            self.delete("user-a", admin="user-a")  # self first
        (self.config_dir / "user-b" / "user.yaml").write_text(
            "id: user-b\nname: X\nmax_level: admin\nprovider: {name: ollama}\nagents: [nova]\n", encoding="utf-8")
        self.delete("user-b")  # user-a stays an administrator
        with self.assertRaises(delete_module.AdminError):
            delete_module.delete_user("user-a", confirm="user-a")  # CLI: nobody else is administrator

    def test_busy_user_is_refused(self) -> None:
        self.service._slot("user-b", "nova").active["chat"] = object()
        with self.assertRaises(InputError):
            self.delete()
        self.assertTrue((self.config_dir / "user-b").exists())

    def test_shared_agent_stays_with_the_other_member_and_billing_moves(self) -> None:
        self.service.share_agent("user-b", "nova", ["user-c"])
        self.assertEqual(all_spaces(self.config_dir)[0].billing_user, "user-b")
        result = self.delete()
        spaces = all_spaces(self.config_dir)
        self.assertEqual(len(spaces), 1)
        self.assertEqual((spaces[0].members, spaces[0].billing_user, spaces[0].agents), (["user-c"], "user-c", ["nova"]))
        self.assertEqual(result["shared_kept"][0][0], "nova")

    def test_space_with_only_this_member_is_removed(self) -> None:
        self.service.share_agent("user-b", "nova", ["user-c"])
        self.service.leave_shared("user-c", "nova")
        space = all_spaces(self.config_dir)[0]
        self.assertEqual(space.members, ["user-b"])
        result = self.delete()
        self.assertEqual(all_spaces(self.config_dir), [])
        self.assertEqual(result["shared_removed"], ["nova"])
        self.assertFalse((self.config_dir / space.id).exists())

    def test_invitations_from_or_to_the_user_are_removed(self) -> None:
        store = self.service._invitation_store()
        mine = store.create(inviter="user-b", invitee="user-c", agent_id="nova", space_id="x")
        to_me = store.create(inviter="user-c", invitee="user-b", agent_id="nova", space_id="x")
        other = store.create(inviter="user-c", invitee="user-a", agent_id="nova", space_id="x")
        result = self.delete()
        self.assertEqual(result["invitations"], 2)
        self.assertIsNone(store.get(mine["id"]))
        self.assertIsNone(store.get(to_me["id"]))
        self.assertIsNotNone(store.get(other["id"]))

    def test_a_folder_outside_the_config_dir_is_refused(self) -> None:
        outside = self.root / "elsewhere"
        outside.mkdir()
        (self.config_dir / "user-b").rename(self.root / "moved")
        (self.config_dir / "user-b").symlink_to(self.root / "moved")
        with self.assertRaises(delete_module.AdminError):
            delete_module.delete_user("user-b", confirm="user-b", actor="user-a")
        self.assertTrue((self.root / "moved" / "user.yaml").exists())

    def test_cli(self) -> None:
        from src.admin import users

        err = io.StringIO()
        with contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(users.main(["delete-user", "--user", "user-b", "--yes", "--confirm", "other"]), 1)
            self.assertEqual(users.main(["delete-user", "--user", "user-b", "--confirm", "user-b"]), 1)
        self.assertTrue((self.config_dir / "user-b").exists())
        out = io.StringIO()
        with contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(out):
            self.assertEqual(users.main(["delete-user", "--user", "user-b", "--yes", "--confirm", "user-b"]), 0)
        self.assertIn("B_KEY", out.getvalue())
        self.assertFalse((self.config_dir / "user-b").exists())


class DeleteUserRouteTests(__import__("tests.permissions.test_permission_ui_api", fromlist=["UiApiFixture"]).UiApiFixture):
    def test_permissions_and_confirmation(self) -> None:
        self.assertEqual(self.admin.request("DELETE", "/api/admin/users/ghost", json={"confirm": "ghost"}).status_code, 404)
        self.assertEqual(self.operator.request("DELETE", "/api/admin/users/user-a", json={"confirm": "user-a"}).status_code, 403)
        self.assertEqual(self.anonymous.request("DELETE", "/api/admin/users/user-a", json={"confirm": "user-a"}).status_code, 401)
        created = self.admin.post("/api/admin/users", json={"id": "user-d", "name": "D"})
        self.assertEqual(created.status_code, 201, created.text)
        self.assertEqual(self.admin.request("DELETE", "/api/admin/users/user-d", json={"confirm": "x"}).status_code, 400)
        response = self.admin.request("DELETE", "/api/admin/users/user-d", json={"confirm": "user-d"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["user"], "user-d")
        self.assertNotIn("user-d", [row["id"] for row in self.admin.get("/api/admin/users").json()])
