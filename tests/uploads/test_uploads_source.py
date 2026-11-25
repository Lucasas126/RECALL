from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace

from src.agent.tools import chat_folder_options
from src.config.levels import Level
from src.core.sources.registry import SourceRegistry
from src.utils.uploads import UploadStore


class UploadsSourceTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        store = UploadStore(self.root)
        self.saved = store.save("user-a", "chat1", "doc.pdf", b"bytes")
        store.save("user-b", "chat1", "secret.txt", b"other")

    def _registry(self, user: str, agent: str) -> SourceRegistry:
        return SourceRegistry.for_agent(user, agent, data_dir=self.root, uploads=True)

    def test_every_agent_of_the_user_reads_the_same_files(self) -> None:
        for agent in ("one", "two"):
            source = self._registry("user-a", agent).get("uploads")
            self.assertEqual(source.read_bytes(f"chat1/{self.saved.name}"), b"bytes")

    def test_other_users_files_are_not_reachable(self) -> None:
        source = self._registry("user-a", "one").get("uploads")
        self.assertEqual(source.list("chat1"), [f"chat1/{self.saved.name}"])
        with self.assertRaises(ValueError):
            source.read_bytes("../../user-b/.uploads/chat1/secret.txt")

    def test_source_is_read_only_and_not_indexed(self) -> None:
        registry = self._registry("user-a", "one")
        source = registry.get("uploads")
        self.assertFalse(source.indexed)
        for call in (lambda: source.write("chat1/x", "t"), lambda: source.write_bytes("chat1/x", b"t"),
                     lambda: source.delete(f"chat1/{self.saved.name}")):
            with self.assertRaises(PermissionError):
                call()
        self.assertTrue(self.saved.exists())

    def test_absent_unless_asked(self) -> None:
        self.assertFalse(SourceRegistry.for_agent("user-a", "one", data_dir=self.root).has("uploads"))

    def test_only_agents_that_can_read_files_get_it(self) -> None:
        def options(level, groups):
            return chat_folder_options(SimpleNamespace(
                folders=[], tool_groups=groups, tools=True, recall_knowledge=False, level=level))["uploads"]

        self.assertTrue(options(Level.READER, ["memory_read"]))
        self.assertTrue(options(Level.READER, ["files_send"]))
        self.assertFalse(options(Level.READER, ["web"]))
        self.assertFalse(options(Level.MINIMAL, ["memory_read"]))  # the level is a ceiling over the group
        self.assertFalse(chat_folder_options(
            SimpleNamespace(folders=[], tool_groups=None, tools=False, recall_knowledge=False, level=Level.ADMIN))["uploads"])
