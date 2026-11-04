from __future__ import annotations

import json
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from fastapi.testclient import TestClient

import test_service_steering as steering
from auth_helpers import login
from src.core.sources.conversations import ConversationsSource
from src.providers.cancellation import GenerationCancelled
from src.utils.i18n import InputError
from src.web.app import create_app


class TruncateStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.source = ConversationsSource(self.root)
        self.chat = self.source.create()
        for role, content in (("user", "one"), ("assistant", "reply one"), ("tool", "x"), ("user", "two"), ("assistant", "reply two")):
            self.source.append(self.chat, role, content)

    def test_keeps_the_head_and_stores_the_removed_turns(self) -> None:
        dropped = self.source.truncate_from(self.chat, 3)
        self.assertEqual([t["content"] for t in dropped], ["two", "reply two"])
        self.assertEqual([t["content"] for t in self.source.load(self.chat)], ["one", "reply one", "x"])
        sidecar = json.loads((self.root / f"{self.chat}.removed.json").read_text(encoding="utf-8"))
        self.assertEqual(len(sidecar), 1)
        self.assertEqual([t["content"] for t in sidecar[0]["turns"]], ["two", "reply two"])
        self.assertTrue(sidecar[0]["removed_at"])
        self.source.truncate_from(self.chat, 0)
        self.assertEqual(self.source.load(self.chat), [])
        self.assertEqual(len(json.loads((self.root / f"{self.chat}.removed.json").read_text(encoding="utf-8"))), 2)
        self.assertEqual(self.source.chats(), [self.chat])  # the sidecar is not a chat

    def test_rejects_non_user_and_out_of_range_targets(self) -> None:
        for index in (1, 2, 5, -1, 99):
            with self.assertRaises(InputError) as raised:
                self.source.truncate_from(self.chat, index)
            self.assertEqual(raised.exception.code, "invalid_edit_target")
        self.assertEqual(len(self.source.load(self.chat)), 5)
        self.assertFalse((self.root / f"{self.chat}.removed.json").exists())

    def test_delete_chat_removes_the_sidecar(self) -> None:
        self.source.truncate_from(self.chat, 3)
        self.source.delete_chat(self.chat)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_generated_title_is_dropped_with_the_first_message_but_a_chosen_one_stays(self) -> None:
        self.source.set_generated_title(self.chat, "Generated")
        self.source.truncate_from(self.chat, 3)
        self.assertEqual(self.source.generated_title(self.chat), "Generated")
        self.source.truncate_from(self.chat, 0)
        self.assertIsNone(self.source.generated_title(self.chat))
        chat = self.source.create()
        self.source.append(chat, "user", "q")
        self.source.rename(chat, "Mine")
        self.source.truncate_from(chat, 0)
        self.assertEqual(self.source.generated_title(chat), "Mine")


class EditMessageServiceTests(steering.SteeringFixture):
    def _history(self) -> list[tuple[str, str]]:
        return [(m["role"], m["content"]) for m in self.service.get_chat("user-one", "agent-one", self.chat)["messages"]]

    def test_edit_regenerates_from_the_truncated_history(self) -> None:
        list(self._send("First"))
        list(self._send("Second"))
        messages = self.service.get_chat("user-one", "agent-one", self.chat)["messages"]
        self.assertEqual([m["index"] for m in messages], [0, 1, 2, 3])
        events = list(self.service.send("user-one", "agent-one", self.chat, "Second, fixed", rewind_to=2))
        self.assertEqual("".join(e.text for e in events if e.kind == "text"), "Answer: Second, fixed")
        self.assertEqual(
            [(m["role"], m["content"]) for m in self._context()],
            [("system", self._context()[0]["content"]), ("user", "First"), ("assistant", "Answer: First"), ("user", "Second, fixed")],
        )
        self.assertEqual(self._history(), [
            ("user", "First"), ("assistant", "Answer: First"), ("user", "Second, fixed"), ("assistant", "Answer: Second, fixed"),
        ])
        removed = json.loads((self.sources[("user-one", "agent-one")].root / f"{self.chat}.removed.json").read_text(encoding="utf-8"))
        self.assertEqual([t["content"] for t in removed[0]["turns"]], ["Second", "Answer: Second"])

    def test_edit_cancels_the_running_reply_and_leaves_no_stale_turns(self) -> None:
        list(self._send("Earlier"))
        started, release = self._gate("Slow again")
        again = self._send("Slow again")
        self.assertTrue(started.wait(2))
        result: list = []
        thread = threading.Thread(
            target=lambda: result.extend(self.service.send("user-one", "agent-one", self.chat, "Quick", rewind_to=0)),
        )
        thread.start()
        token = self.calls[("user-one", "agent-one")][-1]["cancellation"]
        for _ in range(200):
            try:
                token.raise_if_cancelled()
            except GenerationCancelled:
                break
            threading.Event().wait(0.01)
        else:
            self.fail("the running reply was not cancelled")
        release.set()
        thread.join(5)
        self.assertEqual([e.kind for e in again][-1], "interrupted")
        self.assertEqual(self._history(), [("user", "Quick"), ("assistant", "Answer: Quick")])
        self.assertEqual([m["content"] for m in self._context() if m["role"] != "system"], ["Quick"])

    def test_edit_keeps_original_attachments_unless_new_ones_are_sent(self) -> None:
        image = {"type": "image_url", "image_url": {"url": "data:image/png;base64," + "A" * 40}}
        self.sources[("user-one", "agent-one")].append(self.chat, "user", [{"type": "text", "text": "look"}, image])
        self.sources[("user-one", "agent-one")].append(self.chat, "assistant", "ok")
        attachments = self.service._stored_attachments([{"type": "text", "text": "x"}, image])
        self.assertEqual([(a["modality"], a["format"], a["mime_type"]) for a in attachments], [("image", "png", "image/png")])

    def test_invalid_targets_are_rejected_before_anything_changes(self) -> None:
        list(self._send("First"))
        for index in (1, 2, -1):
            with self.assertRaises(InputError) as raised:
                self.service.send("user-one", "agent-one", self.chat, "x", rewind_to=index)
            self.assertEqual(raised.exception.code, "invalid_edit_target")
        self.assertEqual(self._history(), [("user", "First"), ("assistant", "Answer: First")])


class EditMessageWebTests(steering.SteeringFixture):
    def setUp(self) -> None:
        super().setUp()
        self.client = TestClient(create_app(self.service))
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        login(self.client, "user-one")
        self.url = f"/api/agents/agent-one/chats/{self.chat}/messages"

    def test_edit_index_rewinds_and_streams_the_new_reply(self) -> None:
        self.client.post(self.url, json={"content": "First"})
        self.client.post(self.url, json={"content": "Second"})
        history = self.client.get(f"/api/agents/agent-one/chats/{self.chat}").json()["messages"]
        self.assertEqual(history[2]["index"], 2)
        response = self.client.post(self.url, json={"content": "Edited", "edit_index": 2})
        self.assertEqual(response.status_code, 200)
        self.assertIn("Answer: Edited", response.text)
        history = self.client.get(f"/api/agents/agent-one/chats/{self.chat}").json()["messages"]
        self.assertEqual([m["content"] for m in history], ["First", "Answer: First", "Edited", "Answer: Edited"])

    def test_invalid_edit_index_is_a_400_with_a_code(self) -> None:
        self.client.post(self.url, json={"content": "First"})
        response = self.client.post(self.url, json={"content": "x", "edit_index": 1})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["code"], "invalid_edit_target")
        response = self.client.post(self.url, json={"content": "x", "edit_index": 40})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.post(self.url, json={"content": "x", "edit_index": -1}).status_code, 422)


if __name__ == "__main__":
    unittest.main()
