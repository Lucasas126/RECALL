"""Memory edit tools take the agent's per-folder lock (the one the self memory uses), so a remember-task and a normal chat
of the same agent cannot interleave their read-modify-write cycles."""

from __future__ import annotations

import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from time import sleep
from types import SimpleNamespace

from src.agent.toolsets.editing import build_edit_tools
from src.core.sources.registry import SourceRegistry


class MemoryWriteLockTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.sources = SourceRegistry.for_agent("u", "a", data_dir=Path(directory.name) / "data")
        self.files = self.sources.agent_files()
        self.agent = SimpleNamespace(sources=self.sources)
        self.tools = {fn.__name__: fn for fn in build_edit_tools(self.agent)}

    def test_every_edit_tool_waits_for_the_memory_lock(self) -> None:
        self.tools["write"]("mind_palace:note.md", "one\n")
        calls = {
            "write": ("mind_palace:note.md", "two\n"),
            "append": ("mind_palace:note.md", "more"),
            "edit": ("mind_palace:note.md", "one", "uno"),
            "move": ("mind_palace:note.md", "mind_palace:moved.md"),
            "delete": ("mind_palace:note.md",),
        }
        for name, args in calls.items():
            with self.subTest(tool=name):
                self.tools["write"]("mind_palace:note.md", "one\n")
                done = threading.Event()
                results: list[str] = []

                def run(tool=self.tools[name], args=args) -> None:
                    results.append(tool(*args))
                    done.set()

                with self.files.memory_lock():  # a chat of the same agent is in the middle of its own memory write
                    worker = threading.Thread(target=run, daemon=True)
                    worker.start()
                    sleep(0.3)
                    self.assertFalse(done.is_set(), f"{name} did not wait for the lock")
                self.assertTrue(done.wait(10))
                worker.join(5)
                self.assertFalse(results[0].startswith("error"), results[0])

    def test_concurrent_appends_keep_every_line(self) -> None:
        self.tools["write"]("mind_palace:log.md", "")
        append = self.tools["append"]
        # few writers: on Windows the lock primitive (msvcrt LK_LOCK) polls once a second and gives up after ~10 s
        threads = [threading.Thread(target=lambda i=i: append("mind_palace:log.md", f"line {i}")) for i in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        source, rel = self.sources.resolve("mind_palace:log.md")
        lines = [line for line in source.read(rel).splitlines() if line]
        self.assertEqual(sorted(lines), sorted(f"line {i}" for i in range(4)))

    def test_a_registry_without_the_agent_source_still_writes(self) -> None:
        class Bare:
            def __init__(self, inner):
                self.inner = inner

            def __getattr__(self, name):
                if name == "agent_files":
                    raise KeyError("agent")
                return getattr(self.inner, name)

        tools = {fn.__name__: fn for fn in build_edit_tools(SimpleNamespace(sources=Bare(self.sources)))}
        self.assertTrue(tools["write"]("mind_palace:free.md", "x").startswith("wrote"))


if __name__ == "__main__":
    unittest.main()
