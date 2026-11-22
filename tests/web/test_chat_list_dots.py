"""The chat list tells which chats wait for an approval, and the page marks working / ready chats with a dot."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

from tests.chat import test_chat_models as models_tests

_STATIC = Path(__file__).parent.parent.parent / "src" / "web" / "static"


class ChatListApprovalTests(models_tests.ModelFixture):
    def test_each_chat_reports_how_many_approvals_it_waits_on(self) -> None:
        with patch.object(self.service._approvals, "pending", return_value=[{"id": "a"}, {"id": "b"}]):
            listed = {c["id"]: c for c in self.service.list_chats("user-one", "agent-one", self.chat)}
        self.assertEqual(listed[self.chat]["pending_approvals"], 2)

    def test_a_chat_with_nothing_pending_reports_zero(self) -> None:
        listed = {c["id"]: c for c in self.service.list_chats("user-one", "agent-one", self.chat)}
        self.assertEqual(listed[self.chat]["pending_approvals"], 0)


class UnreadStateTests(models_tests.ModelFixture):
    def _listed(self) -> dict:
        return {c["id"]: c for c in self.service.list_chats("user-one", "agent-one", self.chat)}

    def test_a_chat_starts_read_and_the_state_lives_on_the_server(self) -> None:
        self.assertFalse(self._listed()[self.chat]["unread"])
        conversations = self.service._conversations("user-one", "agent-one")
        conversations.set_unread(self.chat, True)
        self.assertTrue(self._listed()[self.chat]["unread"])
        self.assertTrue((conversations.root / f"{self.chat}.unread.json").is_file())

    def test_marking_read_clears_it(self) -> None:
        self.service._conversations("user-one", "agent-one").set_unread(self.chat, True)
        self.assertEqual(self.service.mark_chat_read("user-one", "agent-one", self.chat), {"unread": False})
        self.assertFalse(self._listed()[self.chat]["unread"])

    def test_a_finished_reply_marks_the_chat_unread(self) -> None:
        self.service._mark_unread("user-one", "agent-one", self.chat)
        self.assertTrue(self._listed()[self.chat]["unread"])

    def test_deleting_the_chat_removes_the_mark(self) -> None:
        conversations = self.service._conversations("user-one", "agent-one")
        conversations.set_unread(self.chat, True)
        conversations.delete_chat(self.chat)
        self.assertFalse((conversations.root / f"{self.chat}.unread.json").exists())

    def test_marking_a_vanished_chat_is_ignored(self) -> None:
        self.service._conversations("user-one", "agent-one").set_unread("20200101-000000-aaaaaa", True)


class ChatListDotStaticTests(unittest.TestCase):
    def test_page_marks_working_blinking_and_ready_or_approval_fixed(self) -> None:
        script = (_STATIC / "app.js").read_text(encoding="utf-8")
        css = (_STATIC / "style.css").read_text(encoding="utf-8")
        for needle in ('"approval"', '"ready"', '"working"', "pending_approvals", "chat.unread", "/read"):
            self.assertIn(needle, script)
        self.assertNotIn("localStorage.getItem(READY", script)  # the unread state is the server's, not the browser's
        self.assertIn('#chat-list li[data-dot]::after', css)
        self.assertIn('#chat-list li[data-dot="working"]::after { animation', css)
        self.assertNotIn('[data-dot="ready"]::after { animation', css)  # only the working dot blinks
        self.assertIn("@keyframes working-dot", css)
        self.assertIn('#chat-list li[data-dot="approval"] { background: var(--accent-soft)', css)  # a chat waiting for approval is tinted


if __name__ == "__main__":
    unittest.main()


class ProcessesAndRecoverTests(models_tests.ModelFixture):
    def _process_registry(self):
        from src.agent.toolsets.processes import process_registry
        from src.service.chat import DATA_DIR
        return process_registry(DATA_DIR)

    def test_a_dead_running_record_is_lost_not_listed_as_running(self) -> None:
        registry = self._process_registry()
        folder = registry.folder("user-one")
        import json
        (folder / "pdead0001.json").write_text(json.dumps({
            "id": "pdead0001", "user": "user-one", "agent": "agent-one", "chat_id": self.chat, "backend": "dev",
            "command": "sleep 99", "state": "running", "started": 1.0, "exit_code": None,
        }), encoding="utf-8")
        self.assertEqual(self.service.list_processes("user-one", "agent-one", self.chat), [])
        listed = {c["id"]: c for c in self.service.list_chats("user-one", "agent-one", self.chat)}
        self.assertEqual(listed[self.chat]["processes"], 0)

    def test_page_counts_processes_as_working(self) -> None:
        self.assertIn("chat.processes > 0", (_STATIC / "app.js").read_text(encoding="utf-8"))
