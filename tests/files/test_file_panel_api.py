"""The read-only file panel API: sources, backends, grants, limits, isolation."""
from __future__ import annotations

import base64
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("RECALL_SESSION_SECRET", "x")

from fastapi.testclient import TestClient

from src.core.sources.base import Source
from src.core.sources.registry import SourceRegistry
from src.service.files import FileGrants, FilePanel, clean_path, FileError
from src.terminal.backends import Result
from src.utils.file_kinds import file_kind
from src.web.app import create_app
from tests.support.auth_helpers import login

AGENT = "example_agent"


class FakeSession:
    """A terminal session stand-in over an in-memory backend `atlas` with a fixed file table."""

    def __init__(self, name: str, audit: Path, files: dict[str, bytes]):
        self.name, self.user_id, self.agent_id = name, "user-a", AGENT
        self.audit_file, self.workdir, self.local = audit, "/work", False
        self.files = files
        self.backend = self

    def protected(self):
        return None

    def local_cwd(self, cwd=None):
        return "/work"

    def run(self, command, cwd, timeout, stdin=None):
        path = command.split("\n", 1)[0].removeprefix("f=").strip("'")
        if "ls -lAL" in command:
            lines = ["total 0", "-rw-r--r-- 1 u g 5 1700000000 b.txt", "drwxr-xr-x 2 u g 4096 1700000001 adir"]
            return Result(0, "\n".join(lines) + "\n")
        if path not in self.files:
            return Result(3)
        if "base64" in command:
            return Result(0, base64.b64encode(self.files[path]).decode())
        return Result(0, str(len(self.files[path])))


def _service(users: set[str]):
    service = Mock()
    service.authenticate.side_effect = lambda user, password: {"id": user}
    service.login.side_effect = lambda user: {"id": user}
    service.agent_owner.side_effect = lambda user, agent: user
    return service


def _pdf() -> bytes:
    import pymupdf

    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "hello")
    data = doc.tobytes()
    doc.close()
    return data


class PanelFixture(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.registries: dict[str, SourceRegistry] = {}
        self.sessions: dict[str, dict] = {}
        for user in ("user-a", "user-b"):
            notes = Source("notes", self.tmp / user / "notes")
            self.registries[user] = SourceRegistry({"notes": notes})
            self.sessions[user] = {}
        notes = self.registries["user-a"].get("notes").root
        (notes / "sub").mkdir()
        (notes / "a.md").write_text("# A", encoding="utf-8")
        (notes / "sub" / "z.txt").write_text("z", encoding="utf-8")
        (notes / "b.html").write_text("<p>x</p>", encoding="utf-8")
        (notes / ".hidden.txt").write_text("secret", encoding="utf-8")
        (notes / "doc.pdf").write_bytes(_pdf())
        (self.tmp / "outside.txt").write_text("outside", encoding="utf-8")
        os.symlink(self.tmp / "outside.txt", notes / "link.txt")
        self.grants = FileGrants()

        def load(service, agent, owner, grants, sid):
            return FilePanel(owner, agent, self.registries[owner], self.sessions[owner], grants, sid)

        patcher = patch("src.web.files_api.load_panel", side_effect=load)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.app = create_app(_service({"user-a", "user-b"}), dream_scheduler=False)
        self.client = TestClient(self.app)
        self.client.__enter__()
        self.addCleanup(self.client.__exit__, None, None, None)
        login(self.client, "user-a")

    def get(self, route: str, **params):
        return self.client.get(f"/api/files/{route}", params={"agent": AGENT, **params})


class SourceTests(PanelFixture):
    def test_roots_lists_sources(self) -> None:
        roots = self.get("roots").json()["roots"]
        self.assertEqual(roots, [{"id": "notes", "label": "notes", "kind": "source", "open": True, "granted": True}])

    def test_list_dirs_first_and_no_hidden(self) -> None:
        body = self.get("list", root="notes").json()
        names = [e["name"] for e in body["entries"]]
        self.assertEqual(names[0], "sub")
        self.assertEqual(body["entries"][0]["type"], "dir")
        self.assertNotIn(".hidden.txt", names)
        self.assertNotIn("link.txt", names)  # a link leaving the source is not listed
        self.assertEqual(names[1:], sorted(names[1:], key=str.lower))
        self.assertFalse(body["truncated"])

    def test_list_truncates(self) -> None:
        with patch("src.service.files.FILE_PANEL", Mock(max_file_mib=25, max_entries=2, open_backends=[])):
            body = self.get("list", root="notes").json()
        self.assertEqual(len(body["entries"]), 2)
        self.assertTrue(body["truncated"])

    def test_traversal_refused(self) -> None:
        for path in ("../outside.txt", "/etc/passwd", "sub/../../outside.txt", "link.txt", "~/x"):
            self.assertIn(self.get("content", root="notes", path=path).status_code, (400, 403, 404), path)
            self.assertNotEqual(self.get("content", root="notes", path=path).text, "outside")

    def test_hidden_refused(self) -> None:
        self.assertEqual(self.get("content", root="notes", path=".hidden.txt").status_code, 403)

    def test_unknown_root(self) -> None:
        self.assertEqual(self.get("list", root="nope").status_code, 404)

    def test_content_headers(self) -> None:
        response = self.get("content", root="notes", path="b.html")
        self.assertEqual(response.headers["x-content-type-options"], "nosniff")
        self.assertEqual(response.headers["content-security-policy"], "sandbox")
        self.assertTrue(response.headers["content-disposition"].startswith("inline"))
        download = self.get("content", root="notes", path="a.md", download=1)
        self.assertTrue(download.headers["content-disposition"].startswith("attachment"))
        self.assertNotIn("content-security-policy", download.headers)

    def test_info(self) -> None:
        self.assertEqual(self.get("info", root="notes", path="a.md").json(),
                         {"name": "a.md", "size": 3, "mime": "text/markdown", "kind": "markdown", "pages": None})
        self.assertEqual(self.get("info", root="notes", path="doc.pdf").json()["pages"], 1)

    def test_size_cap(self) -> None:
        with patch("src.service.files.FILE_PANEL", Mock(max_file_mib=1, max_entries=10, open_backends=[])):
            big = self.registries["user-a"].get("notes").root / "big.bin"
            big.write_bytes(b"0" * (1024 * 1024 + 1))
            self.assertEqual(self.get("content", root="notes", path="big.bin").status_code, 413)

    def test_pdf_page_png(self) -> None:
        response = self.get("pdf-page", root="notes", path="doc.pdf", page=1, width=50)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.content.startswith(b"\x89PNG\r\n\x1a\n"))
        self.assertEqual(self.get("pdf-page", root="notes", path="doc.pdf", page=9).status_code, 404)

    def test_user_isolation(self) -> None:
        other = TestClient(self.app)
        other.__enter__()
        self.addCleanup(other.__exit__, None, None, None)
        login(other, "user-b")
        response = other.get("/api/files/content", params={"agent": AGENT, "root": "notes", "path": "a.md"})
        self.assertEqual(response.status_code, 404)

    def test_login_required(self) -> None:
        anonymous = TestClient(self.app)
        self.assertEqual(anonymous.get("/api/files/roots", params={"agent": AGENT}).status_code, 401)


class BackendTests(PanelFixture):
    def setUp(self) -> None:
        super().setUp()
        audit = self.tmp / "audit.jsonl"
        self.audit = audit
        self.sessions["user-a"] = {"atlas": FakeSession("atlas", audit, {"f.txt": b"hi", "p.pdf": _pdf()})}

    def test_approval_required_then_grant(self) -> None:
        roots = self.get("roots").json()["roots"]
        self.assertEqual(roots[-1], {"id": "backend:atlas", "label": "atlas", "kind": "backend", "open": False, "granted": False})
        denied = self.get("list", root="backend:atlas")
        self.assertEqual(denied.status_code, 403)
        self.assertEqual(denied.json(), {"error": "approval_required", "backend": "atlas"})
        self.assertEqual(self.client.post("/api/files/grant", params={"agent": AGENT}, json={"backend": "atlas"}).json(), {"ok": True})
        listing = self.get("list", root="backend:atlas").json()
        self.assertEqual([e["name"] for e in listing["entries"]], ["adir", "b.txt"])
        self.assertEqual(self.get("content", root="backend:atlas", path="f.txt").content, b"hi")
        self.assertIn("file_panel_read", self.audit.read_text(encoding="utf-8"))
        self.assertEqual(self.get("pdf-page", root="backend:atlas", path="p.pdf").content[:4], b"\x89PNG")

    def test_grant_unknown_backend(self) -> None:
        response = self.client.post("/api/files/grant", params={"agent": AGENT}, json={"backend": "ghost"})
        self.assertEqual(response.status_code, 404)

    def test_grant_dropped_on_logout(self) -> None:
        self.client.post("/api/files/grant", params={"agent": AGENT}, json={"backend": "atlas"})
        self.client.post("/api/logout")
        login(self.client, "user-a")
        self.assertEqual(self.get("list", root="backend:atlas").status_code, 403)

    def test_open_backend_needs_no_grant(self) -> None:
        with patch("src.service.files.FILE_PANEL", Mock(max_file_mib=25, max_entries=10, open_backends=["atlas"])):
            self.assertEqual(self.get("content", root="backend:atlas", path="f.txt").status_code, 200)

    def test_protected_refused(self) -> None:
        session = self.sessions["user-a"]["atlas"]
        session.protected = lambda: Mock(reason=lambda path, cwd=".": "a secret" if path.endswith(".env") else None)
        self.grants.grant("x", "user-a", AGENT, "atlas")
        self.client.post("/api/files/grant", params={"agent": AGENT}, json={"backend": "atlas"})
        session.files[".env"] = b"K=1"
        self.assertEqual(self.get("content", root="backend:atlas", path=".env").status_code, 403)


class UnitTests(unittest.TestCase):
    def test_clean_path(self) -> None:
        self.assertEqual(clean_path("a//b/./c/"), "a/b/c")
        for bad in ("../x", "/x", "a/../b", "a\x00b"):
            with self.assertRaises(FileError):
                clean_path(bad)

    def test_grant_expires(self) -> None:
        now = [0.0]
        grants = FileGrants(ttl=10, clock=lambda: now[0])
        grants.grant("s", "u", "a", "b")
        self.assertTrue(grants.granted("s", "u", "a", "b"))
        now[0] = 11
        self.assertFalse(grants.granted("s", "u", "a", "b"))

    def test_file_kind(self) -> None:
        self.assertEqual(file_kind("a.md"), ("markdown", "text/markdown"))
        self.assertEqual(file_kind("a.PNG")[0], "image")
        self.assertEqual(file_kind("a.xyz"), ("binary", "application/octet-stream"))
        self.assertEqual(file_kind("a.py")[0], "code")
        self.assertEqual(file_kind("a.tsv")[0], "csv")


if __name__ == "__main__":
    unittest.main()
