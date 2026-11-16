from __future__ import annotations

import base64
import sys
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch


from tests.support.terminal_helpers import FakeSandbox, Responder, StubBroker, make_session, resolved_agent  # noqa: E402

from src.agent.toolsets import files_send  # noqa: E402
from src.agent.toolsets.fileaccess import FileAccessError, fetch_file  # noqa: E402
from src.agent.toolsets.files_send import MAX_FILE_BYTES, build_files_send_tools  # noqa: E402
from src.agent.toolsets.terminal import build_terminal_tools  # noqa: E402
from src.core.sources.base import Source  # noqa: E402
from src.core.sources.registry import SourceRegistry  # noqa: E402
from src.terminal import files  # noqa: E402

PDF = b"%PDF-1.4\n" + bytes(range(256)) * 8  # binary on purpose: every byte value, NULs, no valid UTF-8


class FakeAgent:
    def __init__(self, root: Path, sessions: dict | None = None) -> None:
        self.sources = SourceRegistry({"notes": Source("notes", root)})
        self.outbox: list[dict] = []
        self._turn_files: list[dict] = []
        self.config = SimpleNamespace(user_id="user-a", id="bot")
        self.terminal_sessions = sessions


class BackendFetchTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.box = FakeSandbox()
        self.box.raw["/w/out.pdf"] = PDF
        self.box.files["/w/note.txt"] = "hola ñ"
        self.session = make_session(self.box, "off")
        self.agent = FakeAgent(self.root, {"box": self.session})
        self.send = build_files_send_tools(self.agent)[0]

    def test_a_binary_file_of_a_backend_arrives_byte_for_byte(self) -> None:
        self.assertIn("queued: out.pdf", self.send("box:/w/out.pdf", "your pdf"))
        item = self.agent.outbox[0]
        self.assertEqual((item["name"], item["modality"], item["mime_type"], item["size"]), ("out.pdf", "document", "application/pdf", len(PDF)))
        self.assertEqual(base64.b64decode(item["data_url"].split(",", 1)[1]), PDF)
        self.assertEqual(item["caption"], "your pdf")

    def test_the_backend_is_asked_the_size_before_anything_is_sent(self) -> None:
        self.box.raw["/w/big.bin"] = b"0" * (MAX_FILE_BYTES + 1)
        self.assertIn("limit is 10 MiB", self.send("box:/w/big.bin"))
        self.assertEqual(self.agent.outbox, [])
        self.assertNotIn("base64", "".join(call.stdin or "" if isinstance(call.stdin, str) else "" for call in self.box.calls))

    def test_missing_and_unknown_things_are_plain_errors(self) -> None:
        self.assertIn("no such file", self.send("box:/w/nope.pdf"))
        self.assertIn("unknown source or backend", self.send("elsewhere:/w/out.pdf"))
        self.assertTrue(self.send("no-colon").startswith("error:"))
        self.assertEqual(self.agent.outbox, [])

    def test_a_source_wins_over_a_backend_with_the_same_name(self) -> None:
        (self.root / "report.md").write_text("# from the source", encoding="utf-8")
        agent = FakeAgent(self.root, {"notes": self.session})
        name, data = fetch_file(agent, "notes:report.md", 1000)
        self.assertEqual((name, data), ("report.md", b"# from the source"))

    def test_sources_still_work_without_any_terminal(self) -> None:
        (self.root / "a.txt").write_text("a", encoding="utf-8")
        agent = FakeAgent(self.root, None)
        self.assertEqual(fetch_file(agent, "notes:a.txt", 100), ("a.txt", b"a"))
        with self.assertRaises(FileAccessError):
            fetch_file(agent, "box:/w/out.pdf", 100)

    def test_the_turn_total_applies_to_backend_files_too(self) -> None:
        with patch.object(files_send, "MAX_TURN_BYTES", 100):
            self.assertIn("total limit", self.send("box:/w/out.pdf"))

    def test_telegram_gets_the_fetched_bytes(self) -> None:
        sent = []
        notifier = SimpleNamespace(
            linked=lambda user: True, send_file=lambda user, name, data, modality, caption=None: sent.append((name, data, modality)),
        )
        with patch.object(files_send.notify_module, "_take_slot", return_value=None):
            send = build_files_send_tools(self.agent, notifier_factory=lambda: notifier)[0]
            self.assertIn("sent out.pdf", send("box:/w/out.pdf", telegram=True))
        self.assertEqual(sent, [("out.pdf", PDF, "document")])


class ApprovalTests(unittest.TestCase):
    """A fetch is a read on that backend: manual mode asks, a denial stops it, a rule can allow it."""

    def setUp(self) -> None:
        directory = TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.box = FakeSandbox()
        self.box.raw["/w/out.pdf"] = PDF

    def agent(self, approvals: str, broker=None):
        session = make_session(self.box, approvals)
        if broker is not None:
            session.broker = broker
        return FakeAgent(self.root, {"box": session}), session

    def test_manual_mode_asks_and_a_refusal_sends_nothing(self) -> None:
        from src.terminal.approvals import ApprovalBroker
        from src.terminal.context import run_context

        broker = ApprovalBroker()
        responder = Responder(broker, answer="deny")
        agent = FakeAgent(self.root, {"box": make_session(self.box, "manual")})
        send = build_files_send_tools(agent)[0]
        with run_context(responder.context()):
            result = send("box:/w/out.pdf")
        self.assertIn("approval", responder.kinds())
        self.assertTrue(result.lower().startswith(("denied", "error")), result)
        self.assertEqual(agent.outbox, [])
        self.assertEqual(self.box.calls, [])

    def test_an_unattended_manual_session_denies_without_running_anything(self) -> None:
        agent = FakeAgent(self.root, {"box": make_session(self.box, "manual", unattended=True)})
        result = build_files_send_tools(agent)[0]("box:/w/out.pdf")
        self.assertTrue(result.lower().startswith(("denied", "error")), result)
        self.assertEqual((agent.outbox, self.box.calls), ([], []))

    def test_off_mode_fetches_and_the_fetch_is_audited(self) -> None:
        with TemporaryDirectory() as audit_dir:
            audit = Path(audit_dir) / "audit.jsonl"
            session = make_session(self.box, "off", audit_file=audit)
            agent = FakeAgent(self.root, {"box": session})
            self.assertIn("queued", build_files_send_tools(agent)[0]("box:/w/out.pdf"))
            lines = audit.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(lines), 1)
            self.assertIn('"send_file"', lines[0])
            self.assertIn("fetch /w/out.pdf", lines[0])

    def test_the_tool_counts_as_a_file_read_for_the_rules(self) -> None:
        from src.terminal.session import tool_kind

        self.assertEqual(tool_kind("send_file"), "file_read")
        self.assertEqual(tool_kind("view_image"), "file_read")


class RegistrationTests(unittest.TestCase):
    def test_build_tools_hands_the_terminal_sessions_to_the_agent(self) -> None:
        from src.agent.tools import build_tools
        from src.config.levels import Level

        with TemporaryDirectory(ignore_cleanup_errors=True) as directory:
            root = Path(directory)
            config = resolved_agent("off").model_copy(update={"level": Level.ADMIN})
            agent = SimpleNamespace(sources=SourceRegistry({"notes": Source("notes", root)}), config=config,
                                    outbox=[], _turn_files=[])
            with patch("src.agent.toolsets.terminal.DATA_DIR", root), \
                    patch("src.agent.toolsets.terminal.make_backend", return_value=FakeSandbox()):
                _, handlers = build_tools(agent, None, ["terminal", "files_send"])
            self.assertEqual(list(agent.terminal_sessions), ["box"])
            self.assertIn("send_file", handlers)
            with patch("src.agent.toolsets.terminal.DATA_DIR", root):
                build_tools(agent, None, ["files_send"])
            self.assertIsNone(agent.terminal_sessions)


if __name__ == "__main__":
    unittest.main()
