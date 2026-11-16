"""Folder and index hardening: reserved roots, allowed roots, protected files, query gating, purge, move."""
from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

from tests.knowledge import test_index
from src.agent.toolsets.editing import build_edit_tools
from src.agent.toolsets.folders import build_folder_tools
from src.config import settings
from src.config.models import FolderConfig
from src.core.sources.folders import FolderSource, folder_path_problem
from src.core.sources.registry import SourceRegistry
from src.service.chat import ChatService
from src.utils.i18n import InputError


def tools_of(funcs) -> dict:
    return {func.tool_spec["function"]["name"]: func for func in funcs}


class ReservedRootTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.config, self.data, self.logs = (self.root / name for name in ("config", "data", "logs"))
        for path in (self.config, self.data, self.logs):
            path.mkdir()
        for name in ("CONFIG_DIR", "DATA_DIR", "LOGS_DIR"):
            patcher = patch.object(settings, name, getattr(self, name.split("_")[0].lower()))
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_reserved_places_are_refused(self) -> None:
        home = Path.home()
        for path in (
            self.config, self.config / "user-a", self.data / "user-a" / "agent", self.logs, self.root,
            "/proc", "/proc/self", "/sys", "/dev", "/", home / ".ssh", home / ".gnupg", home,
        ):
            self.assertIsNotNone(folder_path_problem(path), str(path))

    def test_symlink_into_reserved_root_is_refused(self) -> None:
        link = self.root.parent / f"{self.root.name}-link"
        link.symlink_to(self.config)
        self.addCleanup(link.unlink)
        self.assertIsNotNone(folder_path_problem(link))

    def test_ordinary_folder_is_accepted(self) -> None:
        with TemporaryDirectory(ignore_cleanup_errors=True) as other:
            self.assertIsNone(folder_path_problem(other))

    def test_allowed_roots_limit_folders(self) -> None:
        with TemporaryDirectory(ignore_cleanup_errors=True) as one, TemporaryDirectory(ignore_cleanup_errors=True) as two:
            inside = Path(one) / "vault"
            inside.mkdir()
            self.assertIsNone(folder_path_problem(inside, [one]))
            self.assertIsNone(folder_path_problem(one, [one]))
            self.assertIn("allowed_roots", folder_path_problem(two, [one]))
            with patch.object(settings, "FOLDERS_ALLOWED_ROOTS", [one]):
                self.assertIsNone(folder_path_problem(inside))
                self.assertIsNotNone(folder_path_problem(two))
            self.assertIsNone(folder_path_problem(two, []))

    def test_allowed_roots_never_override_reserved_ones(self) -> None:
        self.assertIsNotNone(folder_path_problem(self.config, [str(self.root)]))

    def test_save_rejects_reserved_folder_with_a_clear_message(self) -> None:
        with self.assertRaises(InputError) as caught:
            ChatService._check_folders([FolderConfig(name="cfg", path=str(self.config))])
        self.assertIn("cfg", str(caught.exception))
        self.assertIn("configuration folder", str(caught.exception))
        with TemporaryDirectory(ignore_cleanup_errors=True) as ok:
            ChatService._check_folders([FolderConfig(name="ok", path=ok)])

    def test_load_skips_reserved_folder_with_a_warning(self) -> None:
        with TemporaryDirectory(ignore_cleanup_errors=True) as ok:
            folders = [FolderConfig(name="cfg", path=str(self.config)), FolderConfig(name="ok", path=ok)]
            with self.assertLogs("recall.sources", level="WARNING") as logs:
                registry = SourceRegistry.for_agent("user-a", "agent", folders=folders, data_dir=self.data)
        self.assertTrue(any("cfg" in line for line in logs.output))
        self.assertFalse(registry.has("cfg"))
        self.assertTrue(registry.has("ok"))


class ProtectedFileTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name).resolve()
        self.vault = self.root / "vault"
        self.vault.mkdir()
        (self.vault / "note.md").write_text("hello", encoding="utf-8")
        (self.vault / "secrets.env").write_text("TOKEN=abc", encoding="utf-8")
        (self.vault / "id_ed25519").write_text("PRIVATE", encoding="utf-8")
        (self.vault / "link.md").symlink_to(self.vault / "secrets.env")
        self.folder = FolderSource("vault", self.vault, readable=True, writable=True)
        self.tools = tools_of(build_folder_tools(SimpleNamespace(sources=SimpleNamespace(folders=lambda: [self.folder]),
                                                                  note_written=lambda _a: None), writable=True))

    def test_resolve_refuses_protected_files(self) -> None:
        for rel in ("secrets.env", "id_ed25519", "link.md"):
            with self.assertRaises(ValueError, msg=rel):
                self.folder.resolve(rel)
        self.assertEqual(self.folder.resolve("note.md").name, "note.md")

    def test_listing_walk_and_index_scan_hide_protected_files(self) -> None:
        self.assertEqual(self.folder.list(), ["note.md"] if "link.md" not in self.folder.list() else self.folder.list())
        self.assertNotIn("secrets.env", self.folder.list())
        self.assertNotIn("id_ed25519", self.folder.list())
        self.assertNotIn("secrets.env", self.folder.walk())
        self.assertEqual(set(self.folder.index_scan({"env", "md", "txt"})) & {"secrets.env", "link.md"}, set())
        self.assertIn("note.md", self.folder.index_scan({"md"}))

    def test_folder_tools_cannot_touch_protected_files(self) -> None:
        for call in (
            lambda: self.tools["folder_read"](path="vault:secrets.env"),
            lambda: self.tools["folder_write"](path="vault:id_ed25519", content="x"),
            lambda: self.tools["folder_write"](path="vault:.env", content="x"),
            lambda: self.tools["folder_read"](path="vault:link.md"),
        ):
            with self.assertRaises(ValueError):
                call()
        self.assertEqual((self.vault / "secrets.env").read_text(encoding="utf-8"), "TOKEN=abc")
        self.assertEqual(self.tools["folder_read"](path="vault:note.md"), "hello")


class QueryGatingAndPurgeTests(test_index.IndexFixture):
    def hits(self, index, query: str = "roses pruning march") -> set[str]:
        return {hit["address"] for hit in index.query(query, 20)}

    def test_query_only_returns_readable_folders(self) -> None:
        self.index.sync()
        self.assertIn("notes:Garden.md", self.hits(self.index))
        closed = SourceRegistry.for_agent("u", "a", data_dir=self.data, folders=self.folders, folders_readable=False)
        gated = self.make_index(sources=closed)
        hits = self.hits(gated)
        self.assertFalse([a for a in hits if a.startswith("notes:")], hits)
        self.assertTrue([a for a in self.hits(gated, "cat tuna") if a.startswith("mind_palace:")])

    def test_query_excluding_a_chat_is_still_gated(self) -> None:
        self.index.sync()
        closed = SourceRegistry.for_agent("u", "a", data_dir=self.data, folders=self.folders, folders_readable=False)
        gated = self.make_index(sources=closed)
        found = {h["address"] for h in gated.query("roses pruning", 20, exclude_chat=self.chat_a)}
        self.assertFalse([a for a in found if a.startswith("notes:")])

    def test_folder_removed_or_unindexed_is_purged_on_reconcile(self) -> None:
        self.index.sync()
        self.assertIn("notes:Garden.md", {m["address"] for m in self.all_chunks().values()})
        unindexed = [FolderConfig(name="notes", path=str(self.notes), indexed=False)]
        self.make_index(sources=self.registry(folders=unindexed)).sync()
        self.assertNotIn("notes:Garden.md", {m["address"] for m in self.all_chunks().values()})
        self.index.sync()  # indexed again
        self.assertIn("notes:Garden.md", {m["address"] for m in self.all_chunks().values()})
        self.make_index(sources=self.registry(folders=[])).sync()
        addresses = {m["address"] for m in self.all_chunks().values()}
        self.assertFalse([a for a in addresses if a.startswith("notes:")])
        self.assertIn("mind_palace:pets.md", addresses)

    def test_temporarily_missing_folder_keeps_its_chunks(self) -> None:
        self.index.sync()
        offline = [FolderConfig(name="notes", path=str(self.root / "not-mounted"))]
        with self.assertLogs("recall.sources", level="WARNING"):
            registry = self.registry(folders=offline)
        self.make_index(sources=registry).sync()
        self.assertIn("notes:Garden.md", {m["address"] for m in self.all_chunks().values()})


class MoveTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.sources = SourceRegistry.for_agent("user-a", "agent", data_dir=self.root / "data")
        palace = self.sources.mind_palace()
        palace.write("a.md", "alpha")
        palace.write("b.md", "beta")
        self.written: list[str] = []
        host = SimpleNamespace(sources=self.sources, note_written=self.written.append)
        self.move = tools_of(build_edit_tools(host))["move"]

    def test_same_destination_is_not_destructive(self) -> None:
        result = self.move(memory_path="mind_palace:a.md", new_path="mind_palace:./a.md")
        self.assertTrue(result.startswith("error"), result)
        self.assertEqual(self.sources.read("mind_palace:a.md"), "alpha")

    def test_existing_destination_is_not_overwritten(self) -> None:
        result = self.move(memory_path="mind_palace:a.md", new_path="mind_palace:b.md")
        self.assertTrue(result.startswith("error") and "already exists" in result, result)
        self.assertEqual(self.sources.read("mind_palace:a.md"), "alpha")
        self.assertEqual(self.sources.read("mind_palace:b.md"), "beta")

    def test_overwrite_flag_replaces_the_destination(self) -> None:
        self.move(memory_path="mind_palace:a.md", new_path="mind_palace:b.md", overwrite=True)
        self.assertEqual(self.sources.read("mind_palace:b.md"), "alpha")
        self.assertFalse(self.sources.mind_palace().resolve("a.md").exists())

    def test_plain_move_and_missing_source(self) -> None:
        self.assertTrue(self.move(memory_path="mind_palace:a.md", new_path="mind_palace:sub/c.md").startswith("moved"))
        self.assertEqual(self.sources.read("mind_palace:sub/c.md"), "alpha")
        self.assertTrue(self.move(memory_path="mind_palace:gone.md", new_path="mind_palace:d.md").startswith("error"))


if __name__ == "__main__":
    unittest.main()
