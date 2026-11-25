"""Administrator panel: create a user, write their provider key, sign them in to Claude and create a first agent."""

from __future__ import annotations

import json
from unittest.mock import patch

import yaml

from src.config import secrets
from src.config.loader import load_user
from src.utils.logger import get_logger
from tests.permissions.test_permission_ui_api import UiApiFixture
from tests.support.auth_helpers import TEST_PASSWORD_HASH  # noqa: F401  (fixtures write user files with it)

KEY = "sk-or-v1-" + "a1b2c3d4e5f6" * 4
NEW = {"id": "user-d", "name": "User D", "language": "en"}


class CreateUserTests(UiApiFixture):
    def create(self, client=None, **body):
        return (client or self.admin).post("/api/admin/users", json={**NEW, **body})

    def test_generated_password_is_returned_once_and_verifies(self) -> None:
        response = self.create()
        self.assertEqual(response.status_code, 201, response.text)
        body = response.json()
        self.assertEqual((body["id"], body["max_level"]), ("user-d", "collaborator"))
        from src.web.auth import verify_password
        self.assertTrue(verify_password(body["generated_password"], load_user("user-d").password_hash))
        rows = {row["id"]: row for row in self.admin.get("/api/admin/users").json()}
        self.assertNotIn("generated_password", rows["user-d"])

    def test_typed_password_is_never_returned(self) -> None:
        body = self.create(password="a-typed-secret-1", max_level="reader").json()
        self.assertIsNone(body["generated_password"])
        self.assertNotIn("a-typed-secret-1", json.dumps(body))
        self.assertEqual(body["max_level"], "reader")

    def test_only_administrators_create_users(self) -> None:
        self.assertEqual(self.create(self.anonymous).status_code, 401)
        response = self.create(self.operator)
        self.assertEqual((response.status_code, response.json()["code"]), (403, "admin_required"))
        self.assertFalse((self.config_dir / "user-d").exists())

    def test_invalid_input_leaves_nothing_behind(self) -> None:
        for body in ({"id": "bad id!"}, {"language": "english"}, {"password": "short"}, {"password": "password123"},
                     {"id": "user-b"}):
            response = self.create(**body)
            self.assertEqual(response.status_code, 400, (body, response.text))
        self.assertEqual(self.create(max_level="root").status_code, 422)
        self.assertFalse((self.config_dir / "user-d").exists())
        self.assertFalse((self.config_dir / "bad id!").exists())

    def test_a_user_file_that_does_not_load_is_removed_with_its_folder(self) -> None:
        with patch("src.admin.users.load_user", side_effect=ValueError("boom")):
            self.assertEqual(self.create().status_code, 400)
        self.assertFalse((self.config_dir / "user-d").exists())

    def test_the_action_is_logged_and_audited_without_the_password(self) -> None:
        with self.assertLogs("recall.service", level="INFO") as logs:
            body = self.create(password="a-typed-secret-1").json()
        self.assertTrue(any("created user user-d" in line for line in logs.output))
        self.assertNotIn("a-typed-secret-1", "\n".join(logs.output))
        record = json.loads((self.data_dir / "user-d" / "admin_audit.jsonl").read_text().splitlines()[0])
        self.assertEqual((record["action"], record["actor"]), ("create_user", "user-a"))
        self.assertNotIn("a-typed-secret-1", json.dumps(record))
        self.assertEqual(body["id"], "user-d")


class ProviderKeyTests(UiApiFixture):
    def setUp(self) -> None:
        super().setUp()
        self.admin.post("/api/admin/users", json=NEW)

    def put(self, client, user="user-d", key=KEY):
        return client.put(f"/api/admin/users/{user}/provider-key", json={"key": key})

    def test_the_key_is_stored_per_user_and_linked_by_name(self) -> None:
        self.admin.post("/api/admin/users", json={**NEW, "id": "user-e"})
        response = self.put(self.admin)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"configured": True, "own": True})
        self.assertEqual(secrets.read_secrets()["OPENROUTER_API_KEY_USER_D"], KEY)
        self.assertNotIn("OPENROUTER_API_KEY_USER_E", secrets.read_secrets())
        data = yaml.safe_load(self.read("user-d/user.yaml"))
        self.assertEqual(data["provider"]["api_key"], "OPENROUTER_API_KEY_USER_D")
        self.assertNotIn(KEY, self.read("user-d/user.yaml"))
        self.assertEqual(yaml.safe_load(self.read("user-e/user.yaml"))["provider"]["api_key"], "OPENROUTER_API_KEY")

    def test_the_value_never_leaks(self) -> None:
        with self.assertLogs("recall.service", level="DEBUG") as logs:
            response = self.put(self.admin)
        self.assertNotIn(KEY, response.text)
        self.assertNotIn(KEY, "\n".join(logs.output))
        audit = (self.data_dir / "user-d" / "admin_audit.jsonl").read_text()
        self.assertIn("set_provider_key", audit)
        self.assertNotIn(KEY, audit)
        self.assertNotIn(KEY, json.dumps(self.admin.get("/api/admin/users").json()))

    def test_the_key_is_refused_in_the_query_string(self) -> None:
        response = self.admin.put(f"/api/admin/users/user-d/provider-key?key={KEY}")
        self.assertEqual(response.status_code, 422)
        self.assertNotIn("OPENROUTER_API_KEY_USER_D", secrets.read_secrets())

    def test_only_administrators_and_known_users(self) -> None:
        self.assertEqual(self.put(self.anonymous).status_code, 401)
        self.assertEqual(self.put(self.operator).status_code, 403)
        self.assertEqual(self.put(self.admin, user="nobody").status_code, 404)
        self.assertEqual(self.put(self.admin, key="short").status_code, 400)
        self.assertNotIn("OPENROUTER_API_KEY_USER_D", secrets.read_secrets())


class ClaudeLoginTests(UiApiFixture):
    def setUp(self) -> None:
        super().setUp()
        self.admin.post("/api/admin/users", json=NEW)

    def test_the_login_is_driven_for_the_target_user_in_their_own_folder(self) -> None:
        with patch("src.service.chat.claude_signin.start", return_value={"url": "https://example.test/x"}) as start:
            response = self.admin.post("/api/admin/users/user-d/claude/login/start")
        self.assertEqual(response.json(), {"url": "https://example.test/x"})
        self.assertEqual(start.call_args.args[0], "user-d")
        folder = start.call_args.args[1].claude_config_dir
        self.assertEqual(folder, (self.config_dir / "user-d" / "claude-login").resolve().as_posix())
        self.assertTrue(load_user("user-d").claude_subscription.claude_config_dir)
        status = {"logged_in": True, "subscription": "pro"}
        with patch("src.service.chat.claude_signin.submit", return_value=status) as submit:
            response = self.admin.post("/api/admin/users/user-d/claude/login/code", json={"code": "abc-123"})
        self.assertEqual(response.json(), status)
        self.assertEqual(submit.call_args.args[0], "user-d")
        audit = (self.data_dir / "user-d" / "admin_audit.jsonl").read_text()
        self.assertIn("claude_login_done", audit)
        self.assertNotIn("abc-123", audit)

    def test_only_administrators(self) -> None:
        for client, status in ((self.anonymous, 401), (self.operator, 403)):
            self.assertEqual(client.post("/api/admin/users/user-d/claude/login/start").status_code, status)
            self.assertEqual(client.post("/api/admin/users/user-d/claude/login/code", json={"code": "x"}).status_code, status)
        self.assertEqual(self.admin.post("/api/admin/users/nobody/claude/login/start").status_code, 404)
        self.assertFalse(load_user("user-d").claude_subscription.claude_config_dir)


class FirstAgentTests(UiApiFixture):
    def setUp(self) -> None:
        super().setUp()
        self.admin.post("/api/admin/users", json={**NEW, "max_level": "operator"})

    def test_templates_are_listed_for_administrators_only(self) -> None:
        names = [row["template"] for row in self.admin.get("/api/admin/agent-templates").json()]
        self.assertIn("default", names)
        self.assertEqual(self.operator.get("/api/admin/agent-templates").status_code, 403)

    def test_an_agent_is_created_from_a_template_with_a_model(self) -> None:
        response = self.admin.post("/api/admin/users/user-d/agents", json={"id": "helper", "template": "guide", "model": "example/model"})
        self.assertEqual(response.status_code, 201, response.text)
        self.assertEqual(load_user("user-d").agents, ["helper"])
        self.assertEqual(yaml.safe_load(self.read("user-d/agents/helper.yaml"))["chat_config"]["model"], "example/model")
        self.assertIn("create_agent", (self.data_dir / "user-d" / "admin_audit.jsonl").read_text())

    def test_a_template_above_the_user_maximum_is_refused(self) -> None:
        self.admin.post("/api/admin/users", json={**NEW, "id": "user-f", "max_level": "reader"})
        response = self.admin.post("/api/admin/users/user-f/agents", json={"id": "helper", "template": "guide"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(load_user("user-f").agents, [])

    def test_permissions(self) -> None:
        body = {"id": "helper"}
        self.assertEqual(self.operator.post("/api/admin/users/user-d/agents", json=body).status_code, 403)
        self.assertEqual(self.admin.post("/api/admin/users/nobody/agents", json=body).status_code, 404)
        self.assertEqual(self.admin.post("/api/admin/users/user-d/agents", json={"id": "bad id"}).status_code, 400)
        self.assertEqual(self.admin.post("/api/admin/users/user-d/agents", json={"id": "x", "template": "nope"}).status_code, 400)
