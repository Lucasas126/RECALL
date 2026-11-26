import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import yaml

from src.admin import spaces, users
from src.config.agents import AgentStore
from src.config.loader import load_user
from src.config.spaces import spaces_of


def user_yaml(user_id: str, level: str, extra: str = "", agents: str = "[]") -> str:
    return (f"id: {user_id}\nname: {user_id}\nmax_level: {level}\npassword_hash: x\nagents: {agents}\n"
            f"provider: {{name: openrouter, api_key: {user_id.upper().replace('-', '_')}_KEY}}\n{extra}")


class AdminSpaceTests(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.config, self.data = self.root / ".config", self.root / "data"
        targets = [("src.admin.users.CONFIG_DIR", self.config), ("src.admin.spaces.CONFIG_DIR", self.config),
                   ("src.admin.spaces.DATA_DIR", self.data), ("src.config.loader.CONFIG_DIR", self.config),
                   ("src.admin.agents.CONFIG_DIR", self.config)]
        for target, value in targets:
            patcher = patch(target, value)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.write("user-a", user_yaml("user-a", "operator", "claude_subscription: {claude_config_dir: claude-login}\n"
                                       "terminal_backends: {atlas: {type: ssh, host: example.invalid, user: u, key_file: k, known_hosts_file: h}}\n"))
        self.write("user-b", user_yaml("user-b", "assistant"))
        self.write("user-c", user_yaml("user-c", "admin", agents="[own]"))
        (self.config / "user-c" / "agents").mkdir()
        (self.config / "user-c" / "agents" / "own.yaml").write_text("id: own\nname: Own\n", encoding="utf-8")

    def write(self, user_id: str, text: str) -> None:
        (self.config / user_id).mkdir(parents=True, exist_ok=True)
        (self.config / user_id / "user.yaml").write_text(text, encoding="utf-8")

    def cli(self, *argv):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = users.main(["space", *argv])
        return code, out.getvalue(), err.getvalue()

    def create(self, *argv) -> str:
        code, out, err = self.cli("create", *argv)
        self.assertEqual(code, 0, err)
        return out.split()[2]

    def test_create_copies_settings_by_name_and_derives_level(self) -> None:
        space_id = self.create("--billing-user", "user-a", "--member", "user-b", "--name", "Team")
        self.assertRegex(space_id, r"^space-[0-9a-f]{8}$")
        space = load_user(space_id)
        self.assertEqual((space.kind, space.members, space.billing_user), ("space", ["user-a", "user-b"], "user-a"))
        self.assertEqual((space.provider.api_key, space.max_level.label), ("USER_A_KEY", "assistant"))
        self.assertIn("atlas", space.terminal_backends)
        self.assertEqual(space.claude_subscription.claude_config_dir, "claude-login")
        self.assertIsNone(space.password_hash)
        raw = yaml.safe_load((self.config / space_id / "user.yaml").read_text(encoding="utf-8"))
        self.assertNotIn("max_level", raw)
        self.assertNotIn("password_hash", raw)
        self.assertEqual(spaces_of("user-b", self.config), [space_id])
        audit = (self.data / space_id / spaces.AUDIT_FILE).read_text(encoding="utf-8")
        self.assertIn('"action": "create"', audit)

    def test_create_needs_real_users(self) -> None:
        code, _, err = self.cli("create", "--billing-user", "nobody")
        self.assertEqual(code, 1)
        self.assertIn("does not exist", err)
        self.assertEqual(list(self.config.glob("space-*")), [])

    def test_list_and_show(self) -> None:
        space_id = self.create("--billing-user", "user-a", "--member", "user-b")
        code, out, _ = self.cli("list")
        self.assertIn(f"{space_id}", out)
        self.assertIn("max_level=assistant", out)
        code, out, _ = self.cli("show", "--space", space_id)
        self.assertEqual(code, 0)
        self.assertIn("kind: space", out)
        self.assertNotIn("password", out.replace("password_hash", ""))

    def test_add_and_remove_member(self) -> None:
        space_id = self.create("--billing-user", "user-a")
        self.assertEqual(self.cli("add-member", "--space", space_id, "--user", "user-b")[0], 0)
        self.assertEqual(load_user(space_id).members, ["user-a", "user-b"])
        self.assertEqual(self.cli("add-member", "--space", space_id, "--user", "user-b")[0], 1)  # twice
        self.assertEqual(self.cli("add-member", "--space", space_id, "--user", "space-00000000")[0], 1)
        self.assertEqual(self.cli("remove-member", "--space", space_id, "--user", "user-b")[0], 0)
        self.assertEqual(load_user(space_id).members, ["user-a"])
        code, _, err = self.cli("remove-member", "--space", space_id, "--user", "user-a")
        self.assertEqual(code, 1)
        self.assertIn("last member", err)

    def test_removing_the_payer_needs_a_new_one_and_copies_its_settings(self) -> None:
        space_id = self.create("--billing-user", "user-a", "--member", "user-b")
        code, _, err = self.cli("remove-member", "--space", space_id, "--user", "user-a")
        self.assertEqual(code, 1)
        self.assertIn("--new-billing-user", err)
        code, _, err = self.cli("remove-member", "--space", space_id, "--user", "user-a", "--new-billing-user", "user-b")
        self.assertEqual(code, 0, err)
        space = load_user(space_id)
        self.assertEqual((space.billing_user, space.provider.api_key, space.terminal_backends), ("user-b", "USER_B_KEY", {}))

    def test_add_member_refuses_agent_id_clash(self) -> None:
        space_id = self.create("--billing-user", "user-a")
        AgentStore(self.config, self.data)  # ensure importable
        (self.config / space_id / "agents").mkdir()
        (self.config / space_id / "agents" / "own.yaml").write_text("id: own\nname: Own\n", encoding="utf-8")
        path = self.config / space_id / "user.yaml"
        path.write_text(path.read_text(encoding="utf-8").replace("agents: []", "agents: [own]"), encoding="utf-8")
        code, _, err = self.cli("add-member", "--space", space_id, "--user", "user-c")
        self.assertEqual(code, 1)
        self.assertIn("clashes", err)
        self.assertEqual(load_user(space_id).members, ["user-a"])

    def test_add_member_refused_when_it_would_lower_the_level_below_an_agent(self) -> None:
        space_id = self.create("--billing-user", "user-a")
        (self.config / space_id / "agents").mkdir()
        (self.config / space_id / "agents" / "op.yaml").write_text("id: op\nname: Op\nlevel: operator\n", encoding="utf-8")
        path = self.config / space_id / "user.yaml"
        path.write_text(path.read_text(encoding="utf-8").replace("agents: []", "agents: [op]"), encoding="utf-8")
        code, _, err = self.cli("add-member", "--space", space_id, "--user", "user-b")
        self.assertEqual(code, 1)
        self.assertIn("op (operator)", err)

    def test_delete_needs_yes_and_no_agents(self) -> None:
        space_id = self.create("--billing-user", "user-a")
        self.assertEqual(self.cli("delete", "--space", space_id)[0], 1)
        self.assertTrue((self.config / space_id).exists())
        self.assertEqual(self.cli("delete", "--space", space_id, "--yes")[0], 0)
        self.assertFalse((self.config / space_id).exists())
        self.assertEqual(self.cli("delete", "--space", "user-a", "--yes")[0], 1)  # not a space id


if __name__ == "__main__":
    unittest.main()
