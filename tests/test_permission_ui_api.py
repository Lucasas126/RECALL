"""Permission levels, web UI step: GET /api/levels, the effective approval rules, rules edited through PUT, and the
administrator's user screen (GET /api/admin/users, PUT /api/admin/users/<id>/max-level)."""

from __future__ import annotations

import unittest
from unittest.mock import patch

import yaml
from admin_helpers import AdminFixture
from auth_helpers import TEST_PASSWORD_HASH, login
from fastapi.testclient import TestClient

from src.agent.tool_groups import TOOL_GROUP_CATALOG
from src.config import ceiling
from src.config.levels import SERVICE_MIN_LEVEL, Level
from src.config.levels_help import SERVICE_TEXT, levels_help
from src.config.loader import load_user
from src.config.models import AgentConfig
from src.terminal.rules import ApprovalRuleList
from src.utils.i18n import messages
from src.web.app import create_app

BACKENDS = (
    "terminal_backends:\n"
    "  sandbox: {type: ssh, kind: sandbox, host: a.example, user: deploy, key_file: /k, known_hosts_file: /h, description: Disposable}\n"
    "  pc: {type: ssh, host: b.example, user: user-b, key_file: /k, known_hosts_file: /h}\n"
    "  rootbox: {type: ssh, host: c.example, user: root, key_file: /k, known_hosts_file: /h}\n"
    "  local: {type: local, workdir: /app}\n"
)
OPS = (
    "id: ops\nname: Ops\nlevel: operator\nchat_config:\n  tool_groups: [terminal]\nterminal:\n  default_backend: pc\n"
    "  backends: {pc: {}}\n"
)
USER_B = (
    "id: user-b\nname: User B\nmax_level: operator\nprovider: {name: ollama}\nagents: [ops, plain]\n"
    "password_hash: '@HASH@'\n" + BACKENDS
)
PLAIN = "id: plain\nname: Plain\nchat_config:\n  tool_groups: [memory_read]\n"
USER_C = "id: user-c\nname: User C\nmax_level: collaborator\nprovider: {name: ollama}\nagents: []\npassword_hash: '@HASH@'\n"


class UiApiFixture(AdminFixture):
    """user-a (admin, agent bot) and user-b (operator, agents ops and plain), user-c (collaborator, no agents)."""

    def setUp(self) -> None:
        super().setUp()
        for user, text in (("user-b", USER_B), ("user-c", USER_C)):
            (self.config_dir / user / "agents").mkdir(parents=True)
            (self.config_dir / user / "user.yaml").write_text(text.replace("@HASH@", TEST_PASSWORD_HASH), encoding="utf-8")
        for name, text in (("ops", OPS), ("plain", PLAIN)):
            (self.config_dir / "user-b" / "agents" / f"{name}.yaml").write_text(text, encoding="utf-8")
        self.anonymous = self.client_for(None)
        self.admin = self.client_for("user-a")
        self.operator = self.client_for("user-b")

    def client_for(self, user: str | None) -> TestClient:
        client = TestClient(create_app(self.service))
        client.__enter__()
        self.addCleanup(client.__exit__, None, None, None)
        if user:
            login(client, user)
        return client

    def user_file(self, user: str) -> dict:
        return yaml.safe_load(self.read(f"{user}/user.yaml"))


class LevelsEndpointTests(UiApiFixture):
    def test_anonymous_is_refused(self) -> None:
        self.assertEqual(self.anonymous.get("/api/levels").status_code, 401)

    def test_it_matches_ceiling_and_levels_for_every_group_and_service(self) -> None:
        body = self.operator.get("/api/levels").json()
        self.assertEqual(body["max_level"], "operator")
        self.assertEqual([item["name"] for item in body["levels"]], [level.label for level in Level])
        self.assertEqual([item["number"] for item in body["levels"]], list(range(6)))
        self.assertEqual(body["groups"], {group: level.label for group, level in ceiling.GROUP_MIN_LEVEL.items()})
        self.assertEqual(body["services"], {key: level.label for key, level in SERVICE_MIN_LEVEL.items()})
        for group in (item["id"] for item in TOOL_GROUP_CATALOG):
            with self.subTest(group=group):
                self.assertEqual(body["groups"][group], ceiling.GROUP_MIN_LEVEL[group].label)
        self.assertEqual(body["mcp"], "collaborator")
        self.assertEqual(body["delegation_targets"], "operator")
        self.assertEqual(body["backend_rules"], {"local": "admin", "ssh_root": "admin", "ssh": "operator"})

    def test_descriptions_are_the_lines_of_the_levels_help(self) -> None:
        body = self.admin.get("/api/levels").json()
        text = levels_help()
        for item in body["levels"]:
            with self.subTest(level=item["name"]):
                self.assertIn(f"- {item['name']} ({item['number']}) ({item['title']}): {item['description']}", text)
                self.assertEqual(item["description"], "; ".join(item["parts"]))
                self.assertEqual(item["tool_groups"], sorted(ceiling.allowed_groups(Level[item["name"].upper()])))

    def test_every_description_part_has_a_spanish_text(self) -> None:
        spanish = messages("es")
        for item in self.admin.get("/api/levels").json()["levels"]:
            for part in item["parts"]:
                with self.subTest(part=part[:50]):
                    self.assertIn(part, spanish)
        for text in SERVICE_TEXT.values():
            self.assertIn(text, spanish)

    def test_max_level_follows_the_user(self) -> None:
        self.assertEqual(self.admin.get("/api/levels").json()["max_level"], "admin")
        self.assertEqual(self.client_for("user-c").get("/api/levels").json()["max_level"], "collaborator")

    def test_the_profile_lists_backend_kinds_and_levels_without_hosts_or_users(self) -> None:
        backends = {item["name"]: item for item in self.operator.get("/api/me").json()["terminal_backends"]}
        self.assertEqual(backends["sandbox"]["kind"], "sandbox")
        self.assertEqual((backends["pc"]["kind"], backends["pc"]["min_level"]), ("machine", "operator"))
        self.assertEqual((backends["rootbox"]["kind"], backends["rootbox"]["min_level"]), ("machine", "admin"))
        self.assertEqual((backends["local"]["kind"], backends["local"]["min_level"]), ("local", "admin"))
        self.assertEqual(set(backends["pc"]), {"name", "type", "description", "kind", "min_level"})


class EffectiveRulesTests(UiApiFixture):
    def setUp(self) -> None:
        super().setUp()
        text = self.read("user-b/user.yaml") + "approval_rules:\n  default: ask\n  rules:\n    - {verdict: deny, command: 'rm *'}\n"
        (self.config_dir / "user-b" / "user.yaml").write_text(text, encoding="utf-8")
        (self.config_dir / "user-b" / "agents" / "ops.yaml").write_text(
            OPS + "approval_rules:\n  rules:\n"
            "    - {verdict: allow, tool: terminal, command: git status, backend: pc}\n"
            "    - {verdict: allow, tool: terminal, command: ls, exact: true, reason: approved always for this agent}\n",
            encoding="utf-8",
        )
        installation = ApprovalRuleList.model_validate({"default": "ask", "rules": [{"verdict": "deny", "command": "mkfs*"}]})
        patcher = patch("src.config.settings.APPROVAL_RULES", installation)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_owner_sees_the_three_layers_and_the_shorthand(self) -> None:
        body = self.operator.get("/api/agents/ops/approval-rules/effective").json()
        layers = {layer["name"]: layer for layer in body["layers"]}
        self.assertEqual([layer["name"] for layer in body["layers"]], ["installation", "user", "agent"])
        self.assertEqual(layers["installation"]["rules"][0]["rule"]["command"], "mkfs*")
        self.assertEqual(layers["user"]["default"], "ask")
        self.assertEqual(layers["user"]["rules"][0]["rule"], {"verdict": "deny", "tool": "any", "command": "rm *"})
        agent_rules = layers["agent"]["rules"]
        self.assertEqual([(rule["index"], rule["learned"]) for rule in agent_rules], [(0, False), (1, True)])
        self.assertEqual(body["shorthand"], {"pc": None})
        self.assertEqual(body["default"], "ask")

    def test_the_shorthand_shows_what_the_agent_file_sets(self) -> None:
        (self.config_dir / "user-b" / "agents" / "ops.yaml").write_text(OPS.replace("{pc: {}}", "{pc: {approvals: smart}}"), encoding="utf-8")
        self.assertEqual(self.operator.get("/api/agents/ops/approval-rules/effective").json()["shorthand"], {"pc": "smart"})

    def test_only_the_owner_reads_it(self) -> None:
        self.assertEqual(self.anonymous.get("/api/agents/ops/approval-rules/effective").status_code, 401)
        for client in (self.admin, self.client_for("user-c")):  # other users, an administrator included
            response = client.get("/api/agents/ops/approval-rules/effective")
            self.assertEqual(response.status_code, 404)
            self.assertIn(response.json()["code"], ("unknown_agent", "no_agents"))


class RuleEditThroughPutTests(UiApiFixture):
    def form(self, **extra) -> dict:
        body = self.operator.get("/api/agents/ops").json()
        body.update(extra)
        return body

    def stored(self, agent: str = "ops") -> dict:
        return yaml.safe_load(self.read(f"user-b/agents/{agent}.yaml"))

    def test_rules_and_the_default_are_saved_and_come_back(self) -> None:
        rules = {"default": "deny", "rules": [
            {"verdict": "allow", "tool": "terminal", "command": "git status", "backend": "pc", "exact": True},
            {"verdict": "ask", "tool": "file_write", "path": "/etc/*", "backend_kind": "machine", "dangerous": True},
        ]}
        response = self.operator.put("/api/agents/ops", json=self.form(approval_rules=rules))
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(self.stored()["approval_rules"]["default"], "deny")
        self.assertEqual(len(self.stored()["approval_rules"]["rules"]), 2)
        self.assertEqual(self.operator.get("/api/agents/ops").json()["approval_rules"]["rules"][1]["path"], "/etc/*")

    def test_a_form_without_rules_keeps_the_stored_ones_and_an_empty_list_clears_them(self) -> None:
        rules = {"rules": [{"verdict": "deny", "command": "rm *"}]}
        self.assertEqual(self.operator.put("/api/agents/ops", json=self.form(approval_rules=rules)).status_code, 200)
        body = self.form()
        del body["approval_rules"]
        self.assertEqual(self.operator.put("/api/agents/ops", json=body).status_code, 200)
        self.assertEqual(len(self.stored()["approval_rules"]["rules"]), 1)
        self.assertEqual(self.operator.put("/api/agents/ops", json=self.form(approval_rules={"rules": []})).status_code, 200)
        self.assertEqual(self.stored()["approval_rules"], {"rules": []})

    def test_invalid_rules_are_refused_with_a_readable_message_and_nothing_is_written(self) -> None:
        before = self.read("user-b/agents/ops.yaml")
        for rule in (
            {"verdict": "maybe"},
            {"verdict": "allow", "tool": "file_read", "command": "ls"},
            {"verdict": "allow", "tool": "terminal", "path": "/tmp"},
            {"verdict": "allow", "exact": True},
            {"verdict": "allow", "bogus": 1},
        ):
            with self.subTest(rule=rule):
                response = self.operator.put("/api/agents/ops", json=self.form(approval_rules={"rules": [rule]}))
                self.assertEqual(response.status_code, 422, response.text)
                self.assertIn("approval_rules", response.text)
        self.assertEqual(self.read("user-b/agents/ops.yaml"), before)

    def test_the_blanket_allow_guardrail_message_reaches_the_form(self) -> None:
        before = self.read("user-b/agents/ops.yaml")
        self.operator.patch("/api/preferences", json={"language": "en"})
        no_shorthand = {"backends": {"pc": {"workdir": "~"}}, "default_backend": "pc"}  # the backend's `approvals` key is absent
        cases = (
            ({"rules": [{"verdict": "allow", "tool": "terminal", "backend": "pc"}]}, {}),
            ({"default": "allow", "rules": []}, {"terminal": no_shorthand}),
        )
        for rules, extra in cases:
            with self.subTest(rules=rules):
                response = self.operator.put("/api/agents/ops", json=self.form(approval_rules=rules, **extra))
                self.assertEqual((response.status_code, response.json()["code"]), (400, "approval_rule_not_allowed"), response.text)
                self.assertIn("needs the level 'admin'", response.json()["detail"])
        self.operator.patch("/api/preferences", json={"language": "es"})
        spanish = self.operator.put("/api/agents/ops", json=self.form(approval_rules=cases[0][0]))
        self.assertIn("necesita el nivel 'admin'", spanish.json()["detail"])
        self.assertEqual(self.read("user-b/agents/ops.yaml"), before)

    def test_a_default_allow_is_not_blanket_while_the_backend_shorthand_decides(self) -> None:
        response = self.operator.put("/api/agents/ops", json=self.form(approval_rules={"default": "allow"}))
        self.assertEqual(response.status_code, 200, response.text)  # the form always writes approvals: manual

    def test_a_narrow_allow_on_a_machine_and_a_blanket_allow_on_a_sandbox_are_accepted(self) -> None:
        narrow = {"rules": [{"verdict": "allow", "tool": "terminal", "command": "ls", "backend": "pc"}]}
        self.assertEqual(self.operator.put("/api/agents/ops", json=self.form(approval_rules=narrow)).status_code, 200)
        sandboxed = self.form(approval_rules={"rules": [{"verdict": "allow", "backend": "sandbox"}]})
        sandboxed["terminal"] = {"backends": {"sandbox": {}}, "default_backend": "sandbox"}
        self.assertEqual(self.operator.put("/api/agents/ops", json=sandboxed).status_code, 200)

    def test_an_administrator_agent_may_blanket_allow_a_machine(self) -> None:
        body = self.admin.get("/api/agents/bot").json()
        body.update(level="admin", approval_rules={"default": "allow"},
                    chat_config={**body["chat_config"], "tool_groups": ["terminal"]},
                    terminal={"backends": {"local": {}}, "default_backend": "local"})
        user = self.read("user-a/user.yaml") + BACKENDS
        self.write_user(user)
        response = self.admin.put("/api/agents/bot", json=body)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(AgentConfig.model_validate(self.admin.get("/api/agents/bot").json()).approval_rules.default, "allow")


class AdminUsersTests(UiApiFixture):
    def put(self, client: TestClient, user: str, level: str):
        return client.put(f"/api/admin/users/{user}/max-level", json={"max_level": level})

    def test_permission_matrix(self) -> None:
        before = self.read("user-c/user.yaml")
        self.assertEqual(self.anonymous.get("/api/admin/users").status_code, 401)
        self.assertEqual(self.put(self.anonymous, "user-c", "reader").status_code, 401)
        for client in (self.operator, self.client_for("user-c")):
            response = client.get("/api/admin/users")
            self.assertEqual((response.status_code, response.json()["code"]), (403, "admin_required"))
            response = self.put(client, "user-c", "reader")
            self.assertEqual((response.status_code, response.json()["code"]), (403, "admin_required"))
        self.assertEqual(self.read("user-c/user.yaml"), before)
        self.assertEqual(self.admin.get("/api/admin/users").status_code, 200)

    def test_the_denial_is_localized(self) -> None:
        self.operator.patch("/api/preferences", json={"language": "es"})
        response = self.operator.get("/api/admin/users")
        self.assertIn("acceso de administrador", response.json()["detail"])

    def test_the_list_shows_users_levels_and_agent_counts(self) -> None:
        users = {row["id"]: row for row in self.admin.get("/api/admin/users").json()}
        self.assertEqual(sorted(users), ["user-a", "user-b", "user-c"])
        self.assertEqual((users["user-a"]["max_level"], users["user-a"]["self"]), ("admin", True))
        self.assertEqual((users["user-b"]["max_level"], users["user-b"]["agent_count"], users["user-b"]["self"]), ("operator", 2, False))
        agents = {agent["id"]: agent["level"] for agent in users["user-b"]["agents"]}
        self.assertEqual(agents, {"ops": "operator", "plain": "reader"})
        self.assertEqual(users["user-b"]["min_max_level"], "operator")
        self.assertEqual((users["user-c"]["agent_count"], users["user-c"]["agents"], users["user-c"]["min_max_level"]), (0, [], "minimal"))
        self.assertNotIn("password_hash", str(users))

    def test_a_broken_user_does_not_hide_the_others(self) -> None:
        (self.config_dir / "user-c" / "user.yaml").write_text("id: user-c\nmax_level: nonsense\n", encoding="utf-8")
        rows = {row["id"]: row for row in self.admin.get("/api/admin/users").json()}
        self.assertIn("error", rows["user-c"])
        self.assertEqual(rows["user-b"]["max_level"], "operator")

    def test_an_administrator_cannot_change_their_own_level(self) -> None:
        before = self.read("user-a/user.yaml")
        response = self.put(self.admin, "user-a", "reader")
        self.assertEqual((response.status_code, response.json()["code"]), (400, "max_level_self_change"))
        self.assertEqual(self.read("user-a/user.yaml"), before)
        self.assertEqual(self.admin.get("/api/me").json()["max_level"], "admin")

    def test_lowering_below_an_agent_is_refused_with_the_conflicting_agents(self) -> None:
        before = self.read("user-b/user.yaml")
        response = self.put(self.admin, "user-b", "collaborator")
        self.assertEqual((response.status_code, response.json()["code"]), (409, "max_level_below_agents"))
        self.assertEqual(response.json()["agents"], [{"id": "ops", "name": "Ops", "level": "operator", "declared": True}])
        self.assertIn("ops (operator)", response.json()["detail"])
        self.assertEqual(self.read("user-b/user.yaml"), before)
        self.admin.patch("/api/preferences", json={"language": "es"})
        spanish = self.put(self.admin, "user-b", "collaborator")
        self.assertIn("por debajo del nivel de estos agentes", spanish.json()["detail"])

    def test_the_level_an_agent_needs_counts_even_when_not_declared(self) -> None:
        (self.config_dir / "user-b" / "agents" / "ops.yaml").write_text(OPS.replace("level: operator\n", ""), encoding="utf-8")
        response = self.put(self.admin, "user-b", "collaborator")
        self.assertEqual(response.status_code, 409)
        self.assertEqual([(a["id"], a["level"], a["declared"]) for a in response.json()["agents"]], [("ops", "operator", False)])

    def test_success_is_written_to_user_yaml_and_effective_on_the_next_request(self) -> None:
        text = self.read("user-c/user.yaml").replace("provider:", "# kept comment\nprovider:")
        (self.config_dir / "user-c" / "user.yaml").write_text(text, encoding="utf-8")
        target = self.client_for("user-c")
        self.assertEqual(target.get("/api/levels").json()["max_level"], "collaborator")
        with self.assertLogs("recall.service", level="INFO") as logs:
            response = self.put(self.admin, "user-c", "OPERATOR")
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual((response.json()["id"], response.json()["max_level"]), ("user-c", "operator"))
        self.assertTrue(any("user-a" in line and "user-c" in line and "collaborator -> operator" in line for line in logs.output))
        self.assertEqual(self.user_file("user-c")["max_level"], "operator")
        self.assertIn("# kept comment", self.read("user-c/user.yaml"))
        self.assertEqual(load_user("user-c").max_level, Level.OPERATOR)
        self.assertEqual(target.get("/api/me").json()["max_level"], "operator")
        self.assertEqual(target.get("/api/levels").json()["max_level"], "operator")
        created = target.post("/api/agents", json={"id": "mine", "name": "Mine", "level": "operator"})
        self.assertEqual(created.status_code, 201, created.text)  # the new ceiling is enforced at once

    def test_lowering_works_when_no_agent_is_in_the_way(self) -> None:
        self.assertEqual(self.put(self.admin, "user-b", "operator").status_code, 200)  # same level: fine
        (self.config_dir / "user-b" / "agents" / "ops.yaml").write_text(
            "id: ops\nname: Ops\nlevel: reader\nchat_config:\n  tool_groups: [memory_read]\n", encoding="utf-8",
        )
        self.assertEqual(self.put(self.admin, "user-b", "reader").status_code, 200)
        self.assertEqual(self.user_file("user-b")["max_level"], "reader")

    def test_bad_requests(self) -> None:
        self.assertEqual(self.put(self.admin, "user-c", "root").status_code, 422)
        self.assertEqual(self.admin.put("/api/admin/users/user-c/max-level", json={}).status_code, 422)
        self.assertEqual(self.admin.put("/api/admin/users/user-c/max-level", json={"max_level": 3}).status_code, 422)
        self.assertEqual(self.put(self.admin, "nobody", "reader").status_code, 404)
        self.assertEqual(self.put(self.admin, "..", "reader").status_code, 404)
        self.assertEqual(self.user_file("user-c")["max_level"], "collaborator")

    def test_no_other_route_sets_the_maximum(self) -> None:
        for client, user in ((self.operator, "user-b"), (self.admin, "user-a")):
            before = self.read(f"{user}/user.yaml")
            self.assertEqual(client.patch("/api/preferences", json={"max_level": "admin"}).status_code, 422)
            self.assertEqual(client.post("/api/agents", json={"id": "x", "name": "X", "max_level": "admin"}).status_code, 422)
            self.assertEqual(client.put("/api/admin/users/" + user + "/max-level", json={"max_level": "admin"}).status_code in (400, 403), True)
            self.assertEqual(self.read(f"{user}/user.yaml"), before)

    def test_the_audit_line_is_a_logger_line_naming_both_users(self) -> None:
        with self.assertLogs("recall.service", level="INFO") as logs:
            self.put(self.admin, "user-c", "reader")
        self.assertTrue(any(line.startswith("INFO") and "max_level" in line for line in logs.output))


if __name__ == "__main__":
    unittest.main()
