from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from pydantic import ValidationError

from src.agent.toolsets.mcp import build_mcp_tools, mcp_tool_name
from src.config.levels import Level
from src.config.models import AgentMcpConfig, McpSettings
from src.providers.mcp import McpError, McpManager, McpTool, UnknownMcpServer

_FAKE_SERVER = str(Path(__file__).resolve().parents[1] / "support" / "fake_mcp_server.py")


def _settings(**server_overrides) -> McpSettings:
    server = {
        "name": "Fake",
        "command": sys.executable,
        "args": [_FAKE_SERVER],
        "env": {"TOKEN_PATH": {"user_file": "tokens.json"}, "SHARED": {"env": "RECALL_TEST_SHARED"}},
        "exclude_tools": ["hidden"],
        "auth": {"args": [_FAKE_SERVER, "auth"], "credential_file": "tokens.json"},
    } | server_overrides
    return McpSettings.model_validate({"connect_timeout_seconds": 60, "servers": {"fake": server}})


class McpSettingsTests(unittest.TestCase):
    def test_env_values_accept_literals_shared_variables_and_user_files(self) -> None:
        env = _settings(env={"A": "1", "B": {"env": "NAME"}, "C": {"user_file": "t.json"}}).servers["fake"].env

        self.assertEqual(env["A"], "1")
        self.assertEqual(env["B"].env, "NAME")
        self.assertEqual(env["C"].user_file, "t.json")

    def test_user_files_cannot_leave_the_user_folder(self) -> None:
        for name in ("../t.json", "a/b.json", "..\\t.json"):
            with self.subTest(name=name), self.assertRaises(ValidationError):
                _settings(env={"A": {"user_file": name}})

    def test_server_ids_are_validated(self) -> None:
        with self.assertRaises(ValidationError):
            McpSettings.model_validate({"servers": {"Bad-Id": {"name": "x", "command": "x"}}})

    def test_agent_tool_selection_rejects_duplicates(self) -> None:
        with self.assertRaises(ValidationError):
            AgentMcpConfig(tools=["a", "a"])


class McpManagerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.env = patch.dict(os.environ, {"RECALL_TEST_SHARED": "shared"})
        self.env.start()
        self.manager = McpManager(_settings(), self.root)

    def tearDown(self) -> None:
        self.manager.close()
        self.env.stop()
        self.tmp.cleanup()

    def _connect(self, user_id: str) -> Path:
        token = self.root / user_id / "mcp" / "fake" / "tokens.json"
        token.parent.mkdir(parents=True, exist_ok=True)
        token.write_text("{}", encoding="utf-8")
        return token

    def test_unknown_server_is_rejected(self) -> None:
        with self.assertRaises(UnknownMcpServer):
            self.manager.status("user-one", "missing")

    def test_tools_require_a_connected_user(self) -> None:
        self.assertFalse(self.manager.status("user-one", "fake")["connected"])
        with self.assertRaises(McpError):
            self.manager.list_tools("user-one", "fake")

    def test_lists_tools_without_hidden_ones_and_marks_read_only(self) -> None:
        self._connect("user-one")

        tools = {tool.name: tool for tool in self.manager.list_tools("user-one", "fake")}

        self.assertEqual(set(tools), {"whoami", "add_event", "fail"})
        self.assertTrue(tools["whoami"].read_only)
        self.assertFalse(tools["add_event"].read_only)
        self.assertIn("title", tools["add_event"].input_schema["properties"])

    def test_each_user_gets_a_private_server_and_credentials(self) -> None:
        first = self._connect("user-one")
        second = self._connect("user-two")

        self.assertEqual(self.manager.call_tool("user-one", "fake", "whoami", {}), str(first.resolve()))
        self.assertEqual(self.manager.call_tool("user-two", "fake", "whoami", {}), str(second.resolve()))
        self.assertEqual(self.manager.call_tool("user-one", "fake", "add_event", {"title": "x"}), "created x")

    def test_tool_errors_and_hidden_tools_raise(self) -> None:
        self._connect("user-one")

        with self.assertRaisesRegex(McpError, "'fail' failed"):
            self.manager.call_tool("user-one", "fake", "fail", {})
        with self.assertRaises(McpError):
            self.manager.call_tool("user-one", "fake", "hidden", {})

    def test_missing_shared_environment_variable_is_reported(self) -> None:
        self._connect("user-one")
        with patch.dict(os.environ, {"RECALL_TEST_SHARED": ""}):
            with self.assertRaisesRegex(McpError, "RECALL_TEST_SHARED"):
                self.manager.list_tools("user-one", "fake")

    def test_missing_command_is_reported(self) -> None:
        manager = McpManager(_settings(command="recall-missing-command"), self.root)
        self._connect("user-one")

        with self.assertRaisesRegex(McpError, "recall-missing-command"):
            manager.list_tools("user-one", "fake")

    def test_sign_in_returns_the_url_and_stores_credentials_for_that_user(self) -> None:
        self.manager.start_auth("user-one", "fake")

        flow = self.manager._auth[("user-one", "fake")]
        self.assertEqual(flow.url, "https://example.test/consent?state=1")
        self.assertTrue(flow.finished.wait(30))
        status = self.manager.status("user-one", "fake")
        self.assertTrue(status["connected"])
        self.assertIsNone(status["error"])
        self.assertFalse(self.manager.status("user-two", "fake")["connected"])

    def test_disconnect_deletes_only_that_users_credentials(self) -> None:
        first = self._connect("user-one")
        second = self._connect("user-two")
        self.manager.list_tools("user-one", "fake")

        status = self.manager.disconnect("user-one", "fake")

        self.assertFalse(status["connected"])
        self.assertFalse(first.exists())
        self.assertTrue(second.exists())


class McpHttpSettingsTests(unittest.TestCase):
    def _http(self, **overrides) -> McpSettings:
        server = {"name": "Remote", "transport": "http", "url": "https://mcp.example.com/mcp", **overrides}
        return McpSettings.model_validate({"servers": {"remote": server}})

    def test_http_server_with_token_auth(self) -> None:
        settings = self._http(auth={"type": "token", "label": "Personal token"})
        self.assertEqual(settings.servers["remote"].auth.label, "Personal token")

    def test_plain_http_only_to_private_hosts(self) -> None:
        for url in ["http://127.0.0.1:8000/mcp", "http://10.1.2.3/mcp", "http://192.168.1.9:8000/mcp",
                    "http://172.20.0.4/mcp", "http://localhost/mcp", "http://atlas:8000/mcp", "http://nas.local/mcp"]:
            self._http(url=url)
        for url in ["http://mcp.example.com/mcp", "http://8.8.8.8/mcp", "http://192.168.1.9.example.com/mcp",
                    "ftp://atlas/mcp", "https://user:secret@mcp.example.com/mcp", "file:///etc/passwd"]:
            with self.assertRaises(ValidationError, msg=url):
                self._http(url=url)

    def test_transport_fields_do_not_mix(self) -> None:
        with self.assertRaises(ValidationError):
            self._http(command="npx")
        with self.assertRaises(ValidationError):
            self._http(auth={"args": ["auth"], "credential_file": "t.json"})
        with self.assertRaises(ValidationError):
            McpSettings.model_validate({"servers": {"s": {"name": "S", "command": "npx", "auth": {"type": "token"}}}})
        with self.assertRaises(ValidationError):
            McpSettings.model_validate({"servers": {"s": {"name": "S"}}})  # stdio without a command


class McpHttpManagerTests(unittest.TestCase):
    TOKEN = "of_good-token"

    @classmethod
    def setUpClass(cls) -> None:
        from tests.support.fake_mcp_http_server import start

        cls.url, cls._stop, cls.valid = start(cls.TOKEN)

    @classmethod
    def tearDownClass(cls) -> None:
        cls._stop()

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        settings = McpSettings.model_validate({"servers": {"remote": {
            "name": "Remote", "transport": "http", "url": self.url, "exclude_tools": ["fail"],
            "auth": {"type": "token", "label": "Access token", "help": "Create one in Settings."},
        }}})
        self.manager = McpManager(settings, self.root)

    def tearDown(self) -> None:
        self.manager.close()
        self._tmp.cleanup()

    def test_status_before_connecting(self) -> None:
        status = self.manager.status("user-one", "remote")
        self.assertFalse(status["connected"])
        self.assertEqual((status["auth_type"], status["token_label"]), ("token", "Access token"))
        with self.assertRaisesRegex(McpError, "Connect Remote"):
            self.manager.list_tools("user-one", "remote")

    def test_token_is_stored_privately_and_tools_work(self) -> None:
        status = self.manager.set_token("user-one", "remote", f"  {self.TOKEN}\n")

        self.assertTrue(status["connected"])
        path = self.manager.state_dir("user-one", "remote") / "token"
        self.assertEqual(path.read_text(encoding="utf-8"), self.TOKEN)
        if os.name == "posix":
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        tools = {t.name: t for t in self.manager.list_tools("user-one", "remote")}
        self.assertEqual(set(tools), {"balance", "add_note"})  # "fail" is hidden by the catalog
        self.assertTrue(tools["balance"].read_only)
        self.assertEqual(self.manager.call_tool("user-one", "remote", "add_note", {"text": "hi"}), "noted hi")
        self.assertFalse(self.manager.status("user-two", "remote")["connected"])

    def test_a_rejected_token_is_not_kept(self) -> None:
        with self.assertRaisesRegex(McpError, "rejected the access token"):
            self.manager.set_token("user-one", "remote", "of_wrong")
        self.assertFalse(self.manager.status("user-one", "remote")["connected"])
        self.assertFalse((self.manager.state_dir("user-one", "remote") / "token").exists())

    def test_malformed_tokens_are_refused(self) -> None:
        for bad in ["", "   ", "two words", "x" * 5000, "line\nbreak"]:
            with self.assertRaises(McpError, msg=repr(bad)):
                self.manager.set_token("user-one", "remote", bad)

    def test_a_token_revoked_later_asks_to_reconnect(self) -> None:
        self.valid.add("of_short-lived")
        self.manager.set_token("user-one", "remote", "of_short-lived")
        self.assertEqual(self.manager.call_tool("user-one", "remote", "balance", {}), "42.00")

        self.valid.discard("of_short-lived")

        with self.assertRaisesRegex(McpError, "rejected the access token"):
            self.manager.call_tool("user-one", "remote", "balance", {})

    def test_disconnect_deletes_the_token(self) -> None:
        self.manager.set_token("user-one", "remote", self.TOKEN)
        self.manager.set_token("user-two", "remote", self.TOKEN)

        status = self.manager.disconnect("user-one", "remote")

        self.assertFalse(status["connected"])
        self.assertTrue(self.manager.status("user-two", "remote")["connected"])

    def test_sign_in_flow_is_not_for_token_servers(self) -> None:
        with self.assertRaisesRegex(McpError, "access token"):
            self.manager.start_auth("user-one", "remote")

    def test_stdio_servers_reject_set_token(self) -> None:
        manager = McpManager(_settings(), self.root)
        with self.assertRaisesRegex(McpError, "does not use an access token"):
            manager.set_token("user-one", "fake", "x")


class McpToolsetTests(unittest.TestCase):
    def setUp(self) -> None:
        self.manager = Mock()
        self.manager.server.return_value = SimpleNamespace(name="Fake")
        self.manager.list_tools.return_value = [
            McpTool("whoami", "Who", {"type": "object", "properties": {}}, True),
            McpTool("add.event", "Add", {"type": "object", "properties": {"title": {"type": "string"}}}, False),
        ]
        self.manager.call_tool.return_value = "done"

    def test_tools_are_prefixed_and_call_the_users_server(self) -> None:
        toolset = build_mcp_tools(self.manager, "user-one", {"fake": AgentMcpConfig()}, level=Level.ADMIN)

        names = [spec["function"]["name"] for spec in toolset.specs]
        self.assertEqual(names, ["fake__whoami", "fake__add_event"])
        result = json.loads(toolset.handlers["fake__add_event"](title="x"))
        self.assertEqual(result, {"untrusted": True, "server": "fake", "content": "done"})
        self.manager.call_tool.assert_called_once_with("user-one", "fake", "add.event", {"title": "x"})

    def test_tool_selection_read_only_and_instructions(self) -> None:
        selection = AgentMcpConfig(tools=["whoami", "add.event"], read_only=True, instructions="Use work calendar")

        toolset = build_mcp_tools(self.manager, "user-one", {"fake": selection}, level=Level.ADMIN)

        self.assertEqual([spec["function"]["name"] for spec in toolset.specs], ["fake__whoami"])
        self.assertIn("Use work calendar", toolset.notes[0])
        self.assertIn("read access", toolset.notes[0])

    def test_unavailable_server_becomes_a_note(self) -> None:
        self.manager.list_tools.side_effect = McpError("Connect {server} in Settings before using it.", server="Fake")

        toolset = build_mcp_tools(self.manager, "user-one", {"fake": AgentMcpConfig()}, level=Level.ADMIN)

        self.assertEqual(toolset.specs, [])
        self.assertIn("Connect Fake in Settings", toolset.notes[0])

    def test_tool_names_fit_the_model_limit(self) -> None:
        self.assertLessEqual(len(mcp_tool_name("a" * 32, "b" * 80)), 64)


if __name__ == "__main__":
    unittest.main()
