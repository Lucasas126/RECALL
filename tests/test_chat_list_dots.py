"""The chat list tells which chats wait for an approval, and the page marks working / ready chats with a dot."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import patch

import test_chat_models as models_tests

_STATIC = Path(__file__).parent.parent / "src" / "web" / "static"


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


if __name__ == "__main__":
    unittest.main()
