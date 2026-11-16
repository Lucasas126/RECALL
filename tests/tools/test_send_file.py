from __future__ import annotations

import asyncio
import base64
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from telegram.error import TelegramError

from src.agent.toolsets import files_send
from src.agent.toolsets.files_send import MAX_FILE_BYTES, build_files_send_tools, modality_of
from src.config import ceiling
from src.config.levels import Level
from src.core.sources.base import Source
from src.core.sources.registry import SourceRegistry
from src.telegram.bot import _send_agent_files


class FakeAgent:
    def __init__(self, root: Path, *, readable: bool = True) -> None:
        self.sources = SourceRegistry({"notes": Source("notes", root, readable=readable)})
        self.outbox: list[dict] = []
        self._turn_files: list[dict] = []
        self.config = SimpleNamespace(user_id="user-a", id="bot")


class SendFileToolTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        (self.root / "sub").mkdir()
        (self.root / "sub" / "recipe.md").write_text("# Pasta\n", encoding="utf-8")
        (self.root / "photo.png").write_bytes(b"\x89PNG" + b"0" * 64)
        self.agent = FakeAgent(self.root)
        self.send = build_files_send_tools(self.agent)[0]

    def test_queues_the_file_with_its_type_and_content(self) -> None:
        self.assertIn("queued", self.send("notes:sub/recipe.md", "your recipe"))
        item = self.agent.outbox[0]
        self.assertEqual((item["name"], item["modality"], item["caption"]), ("recipe.md", "document", "your recipe"))
        self.assertEqual(base64.b64decode(item["data_url"].split(",", 1)[1]), b"# Pasta\n")
        self.send("notes:photo.png")
        self.assertEqual(self.agent.outbox[1]["modality"], "image")
        self.assertNotIn("caption", self.agent.outbox[1])

    def test_nothing_outside_the_readable_sources_goes_out(self) -> None:
        outside = self.root.parent / "outside.txt"
        outside.write_text("secret", encoding="utf-8")
        self.addCleanup(outside.unlink)
        for path in ("notes:../outside.txt", "other:x", "no-colon", "notes:missing.md", "notes:sub"):
            self.assertTrue(self.send(path).startswith("error:"), path)
        self.assertEqual(self.agent.outbox, [])
        unreadable = build_files_send_tools(FakeAgent(self.root, readable=False))[0]
        self.assertIn("not readable", unreadable("notes:photo.png"))

    def test_size_limits_per_file_and_per_message(self) -> None:
        (self.root / "big.bin").write_bytes(b"0" * (MAX_FILE_BYTES + 1))
        self.assertIn("limit", self.send("notes:big.bin"))
        self.assertEqual(self.agent.outbox, [])
        with patch.object(files_send, "MAX_TURN_BYTES", 100):
            self.agent._turn_files = [{"size": 60}]
            self.assertIn("total limit", self.send("notes:photo.png"))

    def test_the_path_is_the_only_input(self) -> None:
        self.assertEqual(set(self.send.tool_spec["function"]["parameters"]["properties"]), {"path", "caption", "telegram"})

    def test_modality_and_level(self) -> None:
        self.assertEqual([modality_of(m) for m in ("image/png", "audio/ogg", "video/mp4", "application/pdf", "text/plain")],
                         ["image", "audio", "video", "document", "document"])
        self.assertEqual(ceiling.GROUP_MIN_LEVEL["files_send"], Level.READER)
        self.assertTrue(ceiling.tool_allowed(Level.READER, "send_file"))
        self.assertFalse(ceiling.tool_allowed(Level.MINIMAL, "send_file"))


class TelegramDeliveryTests(unittest.TestCase):
    def bot(self) -> Mock:
        return SimpleNamespace(
            send_photo=AsyncMock(), send_audio=AsyncMock(), send_video=AsyncMock(),
            send_document=AsyncMock(), send_message=AsyncMock(),
        )

    @staticmethod
    def item(name: str, modality: str, caption: str | None = None) -> dict:
        data = base64.b64encode(b"data").decode()
        return {"name": name, "modality": modality, "data_url": f"data:x/y;base64,{data}", **({"caption": caption} if caption else {})}

    def test_each_type_uses_its_own_method(self) -> None:
        bot = self.bot()
        files = [self.item("a.png", "image", "look"), self.item("b.pdf", "document"), self.item("c.mp3", "audio"), self.item("d.mp4", "video")]
        asyncio.run(_send_agent_files(bot, 7, files, "en"))
        bot.send_photo.assert_awaited_once()
        self.assertEqual(bot.send_photo.await_args.kwargs["caption"], "look")
        self.assertEqual(bot.send_document.await_args.kwargs["filename"], "b.pdf")
        bot.send_audio.assert_awaited_once()
        bot.send_video.assert_awaited_once()

    def test_a_refused_photo_is_retried_as_a_document_and_a_double_failure_is_told(self) -> None:
        bot = self.bot()
        bot.send_photo.side_effect = TelegramError("bad photo")
        asyncio.run(_send_agent_files(bot, 7, [self.item("a.png", "image")], "en"))
        bot.send_document.assert_awaited_once()
        bot.send_message.assert_not_awaited()
        bot.send_document.side_effect = TelegramError("nope")
        asyncio.run(_send_agent_files(bot, 7, [self.item("a.png", "image")], "en"))
        self.assertIn("a.png", bot.send_message.await_args.args[1])


if __name__ == "__main__":
    unittest.main()


class EndToEndTests(unittest.TestCase):
    """The file goes from the tool to the streamed action and to the stored turn, and survives a reload."""

    def setUp(self) -> None:
        from tests.agent.test_checkpoint_turns import RestoreToolContextTests
        self.case = RestoreToolContextTests("test_final_reply_replaces_the_checkpoint")
        self.case.setUp()
        self.addCleanup(self.case.doCleanups)
        directory = TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(directory.cleanup)
        root = Path(directory.name).resolve()
        (root / "report.pdf").write_bytes(b"%PDF-1.4 test")
        self.agent = self.case.agent
        self.agent.sources.get = lambda name: Source("notes", root)  # one readable source for the tool
        self.agent.sources.resolve = lambda address: (Source("notes", root), address.partition(":")[2])
        self.agent._tool_handlers = {"send_file": build_files_send_tools(self.agent)[0]}

    def test_the_stream_event_and_the_stored_turn_carry_the_file(self) -> None:
        provider = self.case.provider
        provider.invoke.side_effect = [
            {"response": "Sending it", "tool_calls": [{"id": "c1", "name": "send_file", "arguments": {"path": "notes:report.pdf"}}]},
            {"response": "Done"},
        ]
        self.agent.invoke("Send me the report", stream=False)
        turn = self.case.turns()[-1]
        self.assertEqual([m["name"] for m in turn["media"]], ["report.pdf"])
        self.assertEqual(turn["media"][0]["modality"], "document")
        # the file is in the stored turn and the model's context holds only the short tool result, not the bytes
        tool_results = [m["content"] for m in self.agent.messages if m.get("role") == "tool"]
        self.assertTrue(tool_results and all("base64" not in text for text in tool_results))
        self.assertEqual(self.agent.outbox, [])

    def test_the_next_reply_starts_with_no_files(self) -> None:
        provider = self.case.provider
        provider.invoke.side_effect = [
            {"response": "x", "tool_calls": [{"id": "c1", "name": "send_file", "arguments": {"path": "notes:report.pdf"}}]},
            {"response": "ok"}, {"response": "plain"},
        ]
        self.agent.invoke("one", stream=False)
        self.agent.invoke("two", stream=False)
        self.assertNotIn("media", self.case.turns()[-1])

    def test_a_reloaded_chat_never_sends_the_file_bytes_back_to_the_model(self) -> None:
        provider = self.case.provider
        provider.invoke.side_effect = [
            {"response": "x", "tool_calls": [{"id": "c1", "name": "send_file", "arguments": {"path": "notes:report.pdf"}}]},
            {"response": "Done"},
        ]
        self.agent.invoke("Send me the report", stream=False)
        reloaded = self.case.reload()
        self.assertNotIn("base64", repr(reloaded.messages))
        self.assertNotIn("JVBER", repr(reloaded.messages))  # the base64 of "%PDF"


class TelegramFromAnyChatTests(unittest.TestCase):
    """`telegram=true` sends the file to the owner's Telegram on its own, whatever the chat is."""

    def setUp(self) -> None:
        from src.agent.toolsets import notify
        notify._sent.clear()
        self.addCleanup(notify._sent.clear)
        directory = TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        (self.root / "note.md").write_text("hi", encoding="utf-8")
        self.agent = FakeAgent(self.root)
        self.notifier = Mock()
        self.notifier.linked.return_value = True
        self.send = build_files_send_tools(self.agent, lambda: self.notifier)[0]

    def test_goes_to_telegram_and_is_not_attached_to_the_chat(self) -> None:
        self.assertIn("sent note.md", self.send("notes:note.md", "for you", True))
        self.notifier.send_file.assert_called_once_with("user-a", "note.md", b"hi", "document", "for you")
        self.assertEqual(self.agent.outbox, [])

    def test_the_default_still_attaches_to_the_chat_and_does_not_touch_telegram(self) -> None:
        self.assertIn("queued", self.send("notes:note.md"))
        self.notifier.send_file.assert_not_called()
        self.assertEqual(len(self.agent.outbox), 1)

    def test_errors_the_agent_can_read_and_the_shared_hourly_limit(self) -> None:
        self.assertIn("no bot token", build_files_send_tools(self.agent, lambda: None)[0]("notes:note.md", "", True))
        self.notifier.linked.return_value = False
        self.assertIn("no Telegram account", self.send("notes:note.md", "", True))
        self.notifier.linked.return_value = True
        self.notifier.send_file.side_effect = RuntimeError("HTTP 500")
        self.assertIn("did not accept", self.send("notes:note.md", "", True))
        from src.agent.toolsets import notify
        self.notifier.send_file.side_effect = None
        for _ in range(notify.MAX_PER_HOUR):
            self.send("notes:note.md", "", True)
        self.assertIn("limit of 30", self.send("notes:note.md", "", True))

    def test_the_tool_asks_for_one_file_at_a_time(self) -> None:
        properties = self.send.tool_spec["function"]["parameters"]["properties"]
        self.assertEqual(set(properties), {"path", "caption", "telegram"})
