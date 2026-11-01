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
from src.config.models import AgentMcpConfig, McpSettings
from src.providers.mcp import McpError, McpManager, McpTool, UnknownMcpServer

_FAKE_SERVER = str(Path(__file__).with_name("fake_mcp_server.py"))


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
        toolset = build_mcp_tools(self.manager, "user-one", {"fake": AgentMcpConfig()})

        names = [spec["function"]["name"] for spec in toolset.specs]
        self.assertEqual(names, ["fake__whoami", "fake__add_event"])
        result = json.loads(toolset.handlers["fake__add_event"](title="x"))
        self.assertEqual(result, {"untrusted": True, "server": "fake", "content": "done"})
        self.manager.call_tool.assert_called_once_with("user-one", "fake", "add.event", {"title": "x"})

    def test_tool_selection_read_only_and_instructions(self) -> None:
        selection = AgentMcpConfig(tools=["whoami", "add.event"], read_only=True, instructions="Use work calendar")

        toolset = build_mcp_tools(self.manager, "user-one", {"fake": selection})

        self.assertEqual([spec["function"]["name"] for spec in toolset.specs], ["fake__whoami"])
        self.assertIn("Use work calendar", toolset.notes[0])
        self.assertIn("read access", toolset.notes[0])

    def test_unavailable_server_becomes_a_note(self) -> None:
        self.manager.list_tools.side_effect = McpError("Connect {server} in Settings before using it.", server="Fake")

        toolset = build_mcp_tools(self.manager, "user-one", {"fake": AgentMcpConfig()})

        self.assertEqual(toolset.specs, [])
        self.assertIn("Connect Fake in Settings", toolset.notes[0])

    def test_tool_names_fit_the_model_limit(self) -> None:
        self.assertLessEqual(len(mcp_tool_name("a" * 32, "b" * 80)), 64)


if __name__ == "__main__":
    unittest.main()
