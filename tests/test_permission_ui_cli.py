"""Admin CLI parity with the web UI: levels in `agent show`, all three rule layers, and `user list`."""

from __future__ import annotations

import unittest

from admin_helpers import AdminFixture

OPS = (
    "id: ops\nname: Ops\nlevel: operator\nchat_config:\n  tool_groups: [terminal]\nterminal:\n  default_backend: pc\n"
    "  backends: {pc: {approvals: smart}}\napproval_rules:\n  default: ask\n  rules:\n"
    "    - {verdict: allow, tool: terminal, command: git status, backend: pc, exact: true}\n"
)
BACKENDS = "terminal_backends:\n  pc: {type: ssh, host: b.example, user: user-a, key_file: /k, known_hosts_file: /h}\n"


class UiCliParityTests(AdminFixture):
    def setUp(self) -> None:
        super().setUp()
        self.write_user(self.read("user-a/user.yaml").replace("agents: [bot]", "agents: [bot, ops]") + BACKENDS)
        self.write_agent("ops", OPS)

    def test_agent_show_ends_with_the_effective_level(self) -> None:
        code, out, err = self.run_cli(["agent", "show", "--user", "user-a", "--agent", "bot"])
        self.assertEqual((code, err), (0, ""))
        self.assertIn("# effective level: assistant (computed, needs assistant for tool group 'memory_edit'); max_level of user-a: admin", out)
        code, out, _ = self.run_cli(["agent", "show", "--user", "user-a", "--agent", "ops"])
        self.assertIn("# effective level: operator (declared); max_level of user-a: admin", out)

    def test_agent_list_shows_levels(self) -> None:
        code, out, _ = self.run_cli(["agent", "list", "--user", "user-a"])
        self.assertIn("ops  name='Ops'  level=operator", out)

    def test_rules_effective_prints_the_three_layers_and_the_shorthand(self) -> None:
        code, out, err = self.run_cli(["agent", "rules", "--user", "user-a", "--agent", "ops", "--effective"])
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("installation layer", out)
        self.assertIn("user layer: no rules", out)
        self.assertIn("agent layer, default ask:", out)
        self.assertIn("allow terminal: git status on pc", out)
        self.assertIn("pc=smart", out)
        self.assertIn("effective default when no rule matches: ask", out)

    def test_user_list_shows_max_level_and_agent_levels(self) -> None:
        code, out, err = self.run_cli(["user", "list"])
        self.assertEqual((code, err), (0, ""), out)
        self.assertIn("user-a  name='User A'  max_level=admin  agents=2: bot=assistant, ops=operator", out)

    def test_user_set_max_level_exists_and_refuses_a_level_below_an_agent(self) -> None:
        code, _, err = self.run_cli(["user", "set", "--user", "user-a", "--set", "max_level=collaborator"])
        self.assertEqual(code, 1)
        self.assertIn("ops (operator)", err)
        code, out, _ = self.run_cli(["user", "set", "--user", "user-a", "--set", "max_level=operator"])
        self.assertEqual(code, 0, out)


if __name__ == "__main__":
    unittest.main()
