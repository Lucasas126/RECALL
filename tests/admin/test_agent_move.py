import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.admin import users
from src.admin.spaces import space_create
from src.config.loader import load_agent_config
from src.config.spaces import all_spaces, clear_spaces_cache
from src.service.agent_move import AgentMover, MoveError


def user_yaml(user_id: str, level: str, agents: str = "[]") -> str:
    return (f"id: {user_id}\nname: {user_id}\nmax_level: {level}\npassword_hash: x\nagents: {agents}\n"
            f"provider: {{name: openrouter, api_key: {user_id.upper().replace('-', '_')}_KEY}}\n")


class AgentMoveTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        self.config, self.data = root / ".config", root / "data"
        for target, value in [("src.admin.users.CONFIG_DIR", self.config), ("src.admin.spaces.CONFIG_DIR", self.config),
                              ("src.admin.spaces.DATA_DIR", self.data), ("src.config.loader.CONFIG_DIR", self.config),
                              ("src.admin.agents.CONFIG_DIR", self.config), ("src.admin.agents.DATA_DIR", self.data)]:
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        clear_spaces_cache()
        self.add_user("user-a", "operator", ["nova"])
        self.add_user("user-b", "operator", ["nova"])
        self.add_user("user-c", "operator")
        for owner in ("user-a", "user-b"):
            (self.config / owner / "agents" / "nova.yaml").write_text("id: nova\nname: Nova\n", encoding="utf-8")
        mem = self.data / "user-a" / "nova" / "memories"
        mem.mkdir(parents=True)
        (mem / "USER.md").write_text("likes tea\n§\nlives far away", encoding="utf-8")
        (self.data / "user-a" / "nova" / "notes.txt").write_text("hello", encoding="utf-8")
        auto = self.data / "user-a" / ".automations"
        auto.mkdir(parents=True)
        (auto / "abc1234567.json").write_text(json.dumps({"id": "abc1234567", "user_id": "user-a", "agent_id": "nova",
                                                          "enabled": True, "status": "active", "next_run": "2030-01-01T00:00:00"}), encoding="utf-8")
        self.mover = AgentMover(self.config, self.data, create_space=space_create)

    def add_user(self, user_id, level, agents=()):
        (self.config / user_id / "agents").mkdir(parents=True, exist_ok=True)
        (self.config / user_id / "user.yaml").write_text(user_yaml(user_id, level, "[" + ", ".join(agents) + "]"), encoding="utf-8")

    def rename_agent_a(self):  # user-b already has `nova`: share another id from user-a
        pass

    def test_share_moves_everything(self) -> None:
        # user-b has its own nova, so use user-c as the member and keep user-b out
        space_id = self.mover.share_agent("user-a", "nova", ["user-c"])
        space = all_spaces(self.config)[0]
        self.assertEqual((space.id, space.members, space.billing_user, space.agents), (space_id, ["user-a", "user-c"], "user-a", ["nova"]))
        self.assertEqual(load_agent_config(space_id, "nova").name, "Nova")
        self.assertFalse((self.config / "user-a" / "agents" / "nova.yaml").exists())
        self.assertFalse((self.data / "user-a" / "nova").exists())
        self.assertEqual((self.data / space_id / "nova" / "notes.txt").read_text(), "hello")
        self.assertEqual((self.data / space_id / "nova" / "memories" / "USER.md").read_text(),
                         "## user-a\nlikes tea\n§\nlives far away\n")
        record = json.loads((self.data / space_id / ".automations" / "abc1234567.json").read_text())
        self.assertEqual((record["user_id"], record["agent_id"], record["enabled"]), (space_id, "nova", True))
        self.assertFalse((self.data / "user-a" / ".automations" / "abc1234567.json").exists())
        self.assertIn("share_agent", (self.data / "user-a" / "admin_audit.jsonl").read_text())
        self.assertIn("share_agent", (self.data / space_id / "admin_audit.jsonl").read_text())

    def test_share_reuses_space_and_checks_clash_and_busy(self) -> None:
        first = self.mover.share_agent("user-a", "nova", ["user-c"])
        self.add_user("user-a", "operator", ["sage"])
        (self.config / "user-a" / "agents" / "sage.yaml").write_text("id: sage\nname: Sage\n", encoding="utf-8")
        self.assertEqual(self.mover.share_agent("user-a", "sage", ["user-c"]), first)
        self.assertEqual(len(all_spaces(self.config)), 1)
        with self.assertRaises(MoveError):  # user-b has its own nova
            AgentMover(self.config, self.data, create_space=space_create).share_agent("user-a", "nova", ["user-b"])
        busy = AgentMover(self.config, self.data, create_space=space_create, is_busy=lambda o, a: True)
        self.add_user("user-a", "operator", ["sage", "atlas"])
        (self.config / "user-a" / "agents" / "atlas.yaml").write_text("id: atlas\nname: Atlas\n", encoding="utf-8")
        with self.assertRaises(MoveError):
            busy.share_agent("user-a", "atlas", ["user-c"])
        self.assertTrue((self.config / "user-a" / "agents" / "atlas.yaml").exists())

    def test_unshare_one_member_moves_back_and_deletes_space(self) -> None:
        space_id = self.mover.share_agent("user-a", "nova", ["user-c"])
        self.cli_remove(space_id, "user-c")
        plan = self.mover.unshare_agent(space_id, "nova", "user-a")
        self.assertEqual(plan, {"user-a": "nova"})
        self.assertTrue((self.data / "user-a" / "nova" / "notes.txt").exists())
        self.assertEqual(all_spaces(self.config), [])
        self.assertEqual(load_agent_config("user-a", "nova").id, "nova")

    def cli_remove(self, space_id, user):
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(users.main(["space", "remove-member", "--space", space_id, "--user", user]), 0)

    def test_unshare_two_members_makes_private_copies(self) -> None:
        space_id = self.mover.share_agent("user-a", "nova", ["user-c"])
        sp = self.data / space_id / "nova"
        (sp / "memories" / "USER.md").write_text("## user-a\nlikes tea\n\n## user-c\nlikes coffee\n", encoding="utf-8")
        self.add_user("user-c", "operator", ["nova"])  # clash: user-c already has nova
        (self.config / "user-c" / "agents" / "nova.yaml").write_text("id: nova\nname: C Nova\n", encoding="utf-8")
        # the space agent was created before the clash appeared; unshare must suffix it
        plan = self.mover.unshare_agent(space_id, "nova", "user-a")
        self.assertEqual(plan, {"user-a": "nova", "user-c": "nova-2"})
        self.assertEqual((self.data / "user-a" / "nova" / "memories" / "USER.md").read_text().strip(), "likes tea")
        self.assertEqual((self.data / "user-c" / "nova-2" / "memories" / "USER.md").read_text().strip(), "likes coffee")
        record = json.loads((self.data / "user-c" / ".automations" / "abc1234567.json").read_text())
        self.assertEqual((record["user_id"], record["agent_id"], record["enabled"]), ("user-c", "nova-2", False))
        self.assertFalse(json.loads((self.data / "user-a" / ".automations" / "abc1234567.json").read_text())["enabled"])
        self.assertEqual(load_agent_config("user-c", "nova-2").id, "nova-2")
        self.assertEqual(all_spaces(self.config), [])
        self.assertFalse((self.data / space_id / "nova").exists())

    def test_cli_share_and_unshare(self) -> None:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = users.main(["agent", "share", "--user", "user-a", "--agent", "nova", "--with", "user-c"])
        self.assertEqual(code, 0, err.getvalue())
        space_id = all_spaces(self.config)[0].id
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
            self.assertEqual(users.main(["agent", "unshare", "--space", space_id, "--agent", "nova"]), 1)
            self.assertEqual(users.main(["agent", "unshare", "--space", space_id, "--agent", "nova", "--yes"]), 0, err.getvalue())

    def test_hooks_and_delegation_targets(self) -> None:
        calls = []
        mover = AgentMover(self.config, self.data, create_space=space_create,
                           on_removed=lambda o, a: calls.append(("gone", o, a)), on_created=lambda o, a: calls.append(("new", o, a)))
        self.add_user("user-a", "operator", ["nova", "sage"])
        (self.config / "user-a" / "agents" / "sage.yaml").write_text(
            "id: sage\nname: Sage\ndelegation:\n  targets:\n    - agent: nova\n", encoding="utf-8")
        space_id = mover.share_agent("user-a", "nova", ["user-c"])
        self.assertEqual(calls, [("gone", "user-a", "nova"), ("new", space_id, "nova")])
        self.assertEqual(load_agent_config("user-a", "sage").delegation.targets, [])
        self.assertIn("drop_delegation_targets", (self.data / "user-a" / "admin_audit.jsonl").read_text())
        calls.clear()
        mover.unshare_agent(space_id, "nova", "user-a")
        self.assertEqual([c[0] for c in calls], ["new", "new", "gone"])

    def test_level_error_names_member_and_capability(self) -> None:
        self.add_user("user-c", "assistant")
        (self.config / "user-a" / "agents" / "nova.yaml").write_text(
            "id: nova\nname: Nova\nchat_config:\n  tool_groups: [terminal]\nterminal:\n  backends: {box: {}}\n", encoding="utf-8")
        with open(self.config / "user-a" / "user.yaml", "a", encoding="utf-8") as handle:
            handle.write("terminal_backends:\n  box: {type: ssh, host: example.invalid, user: u, key_file: k, known_hosts_file: h}\n")
        with self.assertRaises(MoveError) as caught:
            self.mover.share_agent("user-a", "nova", ["user-c"])
        text = str(caught.exception.args) + str(getattr(caught.exception, "values", ""))
        self.assertIn("user-c", text)
        self.assertIn("terminal", text.lower() + "")
        self.assertEqual(all_spaces(self.config), [])


if __name__ == "__main__":
    unittest.main()
