"""After a restart the Telegram chat resumes even when the last active agent is shared through a space."""

from __future__ import annotations

import unittest
from unittest.mock import Mock

from src.telegram.bot import TelegramSession, _last_chat


class SharedResumeTests(unittest.TestCase):
    def test_restart_resumes_the_latest_chat_of_a_space_agent(self) -> None:
        service = Mock()
        service.latest_chat.return_value = {"id": "c1", "agent_id": "nova", "shared": True, "space_id": "space-0a1b2c3d"}
        found = _last_chat(TelegramSession("user-a", "sage"), service)
        self.assertEqual(found, (service.latest_chat.return_value, "nova"))
        service.latest_chat.assert_called_once_with("user-a", origin="telegram")

    def test_a_live_session_reads_the_chat_from_the_space_owner(self) -> None:
        from datetime import datetime, timezone
        service = Mock()
        service.agent_owner.return_value = "space-0a1b2c3d"
        service.chat_summary.return_value = {"id": "c1"}
        session = TelegramSession("user-a", "nova", "c1")
        session.last_message_at = datetime.now(timezone.utc)
        self.assertEqual(_last_chat(session, service), ({"id": "c1"}, "nova"))
        service.chat_summary.assert_called_once_with("space-0a1b2c3d", "nova", "c1")


if __name__ == "__main__":
    unittest.main()
