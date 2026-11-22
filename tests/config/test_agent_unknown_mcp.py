"""An agent that enables an MCP server missing from the catalog still loads, without that server."""

from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from tests.support.admin_helpers import AdminFixture

from src.config.loader import load_agent, load_agent_config
from src.config.models import McpSettings

CATALOG = McpSettings.model_validate({"servers": {"known": {"name": "Known", "command": "npx"}}})


class UnknownMcpLoadTests(AdminFixture):
    def setUp(self) -> None:
        super().setUp()
        environment = patch.dict(os.environ, {"USER_A_KEY_NAME": "secret"})
        environment.start()
        self.addCleanup(environment.stop)
        catalog = patch("src.config.loader.current_mcp_settings", lambda: CATALOG)
        catalog.start()
        self.addCleanup(catalog.stop)
        self.write_agent("bot", "id: bot\nname: Bot\nchat_config:\n  mcp:\n    known: {}\n    gone: {}\n")

    def test_unknown_servers_are_dropped_and_logged_but_the_file_is_kept(self) -> None:
        before = self.read("user-a/agents/bot.yaml")
        with self.assertLogs("recall.config", level="WARNING") as logs:
            resolved = load_agent("user-a", "bot")
        self.assertEqual(list(resolved.mcp), ["known"])
        self.assertIn("bot", logs.output[0])
        self.assertIn("gone", logs.output[0])
        self.assertEqual(self.read("user-a/agents/bot.yaml"), before)
        self.assertEqual(list(load_agent_config("user-a", "bot").chat_config.mcp), ["known", "gone"])


if __name__ == "__main__":
    unittest.main()
