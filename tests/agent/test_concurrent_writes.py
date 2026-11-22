"""Several chats, tasks and dreams of one agent writing the same files at once: changes queue on the agent's lock, a
whole-file write never silently drops a change made since the writer last read the file, and files are replaced
atomically."""

from __future__ import annotations

import multiprocessing
import os
import stat
import threading
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from time import sleep
from types import SimpleNamespace
from unittest import mock

from src.agent.toolsets.editing import build_edit_tools
from src.agent.toolsets.folders import build_folder_tools
from src.agent.toolsets.memory import build_memory_tools
from src.agent.toolsets.self_files import build_self_tools
from src.config.models import FolderConfig
from src.core.sources.registry import SourceRegistry
from src.utils.files import atomic_replace_bytes, exclusive_file_lock

SKILL = "---\nname: demo\ndescription: a demo skill\n---\n\n# Demo\n\nstep one\n"


class _Chat:
    """One chat of the agent: its own registry, tools and read ledger, over the agent's shared data folder."""

    def __init__(self, data_dir: Path, folders=(), soul: str = "") -> None:
        self.sources = SourceRegistry.for_agent(
            "u", "a", data_dir=data_dir, folders=folders, folders_readable=bool(folders), folders_writable=bool(folders),
        )
        self.agent = SimpleNamespace(sources=self.sources, profile=SimpleNamespace(soul=soul))
        tools = [*build_memory_tools(self.agent), *build_edit_tools(self.agent), *build_self_tools(self.agent)]
        if folders:
            tools += build_folder_tools(self.agent, writable=True)
        self.tools = {fn.__name__: fn for fn in tools}

    def __getattr__(self, name):
        return self.tools[name]


class _Base(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.data = self.root / "data"
        self.a = _Chat(self.data)
        self.b = _Chat(self.data)

    def text(self, address: str) -> str:
        return self.a.sources.read(address)


class WholeFileWriteTests(_Base):
    def test_a_new_file_is_written_without_a_read(self) -> None:
        self.assertTrue(self.a.write("mind_palace:new.md", "hello").startswith("wrote"))

    def test_overwriting_an_unread_file_is_refused(self) -> None:
        self.a.write("mind_palace:note.md", "one")
        result = self.b.write("mind_palace:note.md", "two")
        self.assertIn("have not read it", result)
        self.assertEqual(self.text("mind_palace:note.md"), "one")

    def test_a_write_after_a_read_succeeds(self) -> None:
        self.a.write("mind_palace:note.md", "one")
        self.b.read("mind_palace:note.md")
        self.assertTrue(self.b.write("mind_palace:note.md", "two").startswith("wrote"))
        self.assertEqual(self.text("mind_palace:note.md"), "two")

    def test_the_writer_can_write_its_own_file_again(self) -> None:
        self.a.write("mind_palace:note.md", "one")
        self.assertTrue(self.a.write("mind_palace:note.md", "two").startswith("wrote"))

    def test_an_append_from_another_chat_is_not_lost(self) -> None:
        self.a.write("mind_palace:note.md", "base\n")
        self.b.read("mind_palace:note.md")  # B reads, thinks for a while...
        self.a.append("mind_palace:note.md", "added by A")  # ...meanwhile A appends
        result = self.b.write("mind_palace:note.md", "rewritten by B\n")
        self.assertIn("changed since you last read it", result)
        self.assertIn("added by A", self.text("mind_palace:note.md"))
        self.b.read("mind_palace:note.md")  # B reads again and redoes its change
        self.assertTrue(self.b.write("mind_palace:note.md", "rewritten by B\nadded by A\n").startswith("wrote"))

    def test_own_append_and_edit_keep_the_view_current(self) -> None:
        self.a.write("mind_palace:note.md", "one\n")
        self.a.append("mind_palace:note.md", "two")
        self.a.edit("mind_palace:note.md", "one", "uno")
        self.assertTrue(self.a.write("mind_palace:note.md", "fresh").startswith("wrote"))

    def test_an_edit_over_a_stale_view_does_not_refresh_it(self) -> None:
        self.a.write("mind_palace:note.md", "one\ntwo\n")
        self.b.read("mind_palace:note.md")
        self.a.append("mind_palace:note.md", "three")
        self.assertTrue(self.b.edit("mind_palace:note.md", "one", "uno").startswith("edited"))  # targeted: safe
        # B never saw "three": a later whole-file write by B must still be refused
        self.assertIn("Nothing was written", self.b.write("mind_palace:note.md", "uno\ntwo\n"))

    def test_rewriting_the_same_bytes_is_not_a_change(self) -> None:
        self.a.write("mind_palace:note.md", "same")
        self.b.read("mind_palace:note.md")
        self.a.read("mind_palace:note.md")
        self.a.write("mind_palace:note.md", "same")
        self.assertTrue(self.b.write("mind_palace:note.md", "next").startswith("wrote"))

    def test_a_vanished_file_can_be_written_again(self) -> None:
        self.a.write("mind_palace:note.md", "one")
        self.b.read("mind_palace:note.md")
        self.a.delete("mind_palace:note.md")
        self.assertTrue(self.b.write("mind_palace:note.md", "two").startswith("wrote"))


class DeleteAndMoveTests(_Base):
    def test_deleting_a_file_changed_since_it_was_read_is_refused(self) -> None:
        self.a.write("mind_palace:note.md", "one")
        self.b.read("mind_palace:note.md")
        self.a.append("mind_palace:note.md", "two")
        self.assertIn("Nothing was deleted", self.b.delete("mind_palace:note.md"))
        self.assertTrue(self.a.sources.mind_palace().exists("note.md"))

    def test_deleting_an_unread_or_current_file_works(self) -> None:
        self.a.write("mind_palace:x.md", "x")
        self.assertTrue(self.b.delete("mind_palace:x.md").startswith("deleted"))
        self.a.write("mind_palace:y.md", "y")
        self.assertTrue(self.a.delete("mind_palace:y.md").startswith("deleted"))

    def test_a_moved_file_keeps_the_view_of_its_mover(self) -> None:
        self.a.write("mind_palace:old.md", "one")
        self.a.move("mind_palace:old.md", "mind_palace:new.md")
        self.assertTrue(self.a.write("mind_palace:new.md", "two").startswith("wrote"))
        self.assertIn("have not read it", self.b.write("mind_palace:new.md", "three"))

    def test_an_edit_on_a_moved_file_fails_loudly(self) -> None:
        self.a.write("mind_palace:old.md", "one")
        self.b.read("mind_palace:old.md")
        self.a.move("mind_palace:old.md", "mind_palace:new.md")
        with self.assertRaises(FileNotFoundError):
            self.b.edit("mind_palace:old.md", "one", "uno")
        self.assertEqual(self.text("mind_palace:new.md"), "one")


class SelfFileTests(_Base):
    def test_patches_of_one_skill_from_two_chats_both_land(self) -> None:
        self.a.skill_manage("create", "demo", content=SKILL)
        self.a.skill_manage("patch", "demo", old_string="step one", new_string="step one\nstep two")
        self.b.skill_manage("patch", "demo", old_string="# Demo", new_string="# Demo skill")
        text = self.a.sources.agent_files().skill_text("demo")
        self.assertIn("step two", text)
        self.assertIn("# Demo skill", text)

    def test_a_skill_edit_over_a_stale_view_is_refused(self) -> None:
        self.a.skill_manage("create", "demo", content=SKILL)
        self.b.skill_view("demo")
        self.a.skill_manage("patch", "demo", old_string="step one", new_string="step one\nstep two")
        result = self.b.skill_manage("edit", "demo", content=SKILL.replace("step one", "only step"))
        self.assertIn("changed since", result)
        self.assertIn("skill_view('demo')", result)
        self.assertIn("step two", self.a.sources.agent_files().skill_text("demo"))
        self.b.skill_view("demo")
        self.assertTrue(self.b.skill_manage("edit", "demo", content=SKILL).startswith("edit done"))

    def test_a_skill_file_write_needs_a_current_view(self) -> None:
        self.a.skill_manage("create", "demo", content=SKILL)
        self.a.skill_manage("write_file", "demo", file_path="references/n.md", file_content="one")
        self.assertIn("have not read it", self.b.skill_manage("write_file", "demo", file_path="references/n.md", file_content="two"))
        self.b.skill_view("demo", "references/n.md")
        self.assertTrue(self.b.skill_manage("write_file", "demo", file_path="references/n.md", file_content="two").startswith("write_file done"))

    def test_soul_replace_uses_the_prompt_as_its_view(self) -> None:
        files = self.a.sources.agent_files()
        files.write_soul("original soul")
        chat = _Chat(self.data, soul="original soul")
        self.assertTrue(chat.soul_edit("replace", content="new soul").startswith("SOUL.md updated"))
        stale = _Chat(self.data, soul="original soul")  # started before the change above
        self.assertIn("changed since", stale.soul_edit("replace", content="stale soul"))
        self.assertEqual(files.soul(), "new soul")
        self.assertTrue(stale.soul_edit("patch", old_string="new", new_string="newer").startswith("SOUL.md updated"))

    def test_soul_replace_after_reading_the_file(self) -> None:
        files = self.a.sources.agent_files()
        files.write_soul("v1")
        chat = _Chat(self.data, soul="v0")
        self.assertIn("read('agent:SOUL.md')", chat.soul_edit("replace", content="v2"))
        chat.read("agent:SOUL.md")
        self.assertTrue(chat.soul_edit("replace", content="v2").startswith("SOUL.md updated"))

    def test_soul_patch_waits_for_the_agent_lock(self) -> None:
        files = self.a.sources.agent_files()
        files.write_soul("one")
        done = threading.Event()
        with files.memory_lock():
            worker = threading.Thread(target=lambda: (self.b.soul_edit("patch", old_string="one", new_string="two"), done.set()))
            worker.start()
            sleep(0.3)
            self.assertFalse(done.is_set())
        self.assertTrue(done.wait(10))
        self.assertEqual(files.soul(), "two")


class FolderTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        vault = root / "vault"
        vault.mkdir()
        (vault / "plan.md").write_text("plan\n", encoding="utf-8")
        patcher = mock.patch("src.config.settings.DATA_DIR", root / "data")
        patcher.start()
        self.addCleanup(patcher.stop)
        folders = [FolderConfig(name="notes", path=str(vault))]
        self.a = _Chat(root / "data", folders)
        self.b = _Chat(root / "data", folders)
        self.vault = vault

    def test_folder_write_follows_the_same_rule(self) -> None:
        self.assertIn("have not read it", self.a.folder_write("notes:plan.md", "mine"))
        self.a.folder_read("notes:plan.md")
        self.b.folder_append("notes:plan.md", "from B")
        self.assertIn("changed since", self.a.folder_write("notes:plan.md", "mine"))
        self.assertIn("from B", (self.vault / "plan.md").read_text(encoding="utf-8"))

    def test_the_folder_lock_lives_outside_the_folder(self) -> None:
        self.a.folder_append("notes:plan.md", "x")
        self.assertEqual(sorted(p.name for p in self.vault.iterdir()), ["plan.md"])

    def test_concurrent_folder_appends_keep_every_line(self) -> None:
        threads = [
            threading.Thread(target=lambda i=i: (self.a if i % 2 else self.b).folder_append("notes:plan.md", f"line {i}"))
            for i in range(8)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        lines = (self.vault / "plan.md").read_text(encoding="utf-8").splitlines()
        self.assertEqual(sorted(lines), sorted(["plan", *(f"line {i}" for i in range(8))]))


def _retrying_writer(data_dir: str, name: str, rounds: int) -> None:
    """A chat that adds a line by reading the whole file and writing it back, retrying when it was refused."""
    chat = _Chat(Path(data_dir))
    for number in range(rounds):
        while True:
            current = chat.read("mind_palace:shared.md")  # `read` shows the lines without the last newline
            if chat.write("mind_palace:shared.md", (current + "\n" if current else "") + f"{name}-{number}").startswith("wrote"):
                break


class StressTests(_Base):
    """Writers racing on one file: with read-then-write retries no line is ever lost."""

    ROUNDS = 15

    def test_threads_never_lose_a_write(self) -> None:
        self.a.write("mind_palace:shared.md", "")
        names = [f"t{i}" for i in range(6)]
        threads = [threading.Thread(target=_retrying_writer, args=(str(self.data), name, self.ROUNDS)) for name in names]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(120)
        lines = self.text("mind_palace:shared.md").splitlines()
        self.assertEqual(sorted(lines), sorted(f"{n}-{i}" for n in names for i in range(self.ROUNDS)))

    @unittest.skipIf(os.name == "nt", "fork start method: POSIX only")
    def test_processes_never_lose_a_write(self) -> None:
        self.a.write("mind_palace:shared.md", "")
        context = multiprocessing.get_context("fork")
        names = [f"p{i}" for i in range(4)]
        processes = [context.Process(target=_retrying_writer, args=(str(self.data), name, self.ROUNDS)) for name in names]
        for process in processes:
            process.start()
        for process in processes:
            process.join(120)
            self.assertEqual(process.exitcode, 0)
        lines = self.text("mind_palace:shared.md").splitlines()
        self.assertEqual(sorted(lines), sorted(f"{n}-{i}" for n in names for i in range(self.ROUNDS)))

    def test_mixed_appends_edits_and_writes_lose_nothing(self) -> None:
        self.a.write("mind_palace:mix.md", "counter: 0\n")
        appended = [f"note {i}" for i in range(10)]

        def appender() -> None:
            chat = _Chat(self.data)
            for line in appended:
                chat.append("mind_palace:mix.md", line)

        def editor() -> None:
            chat = _Chat(self.data)
            for number in range(10):
                while not chat.edit("mind_palace:mix.md", f"counter: {number}\n", f"counter: {number + 1}\n").startswith("edited"):
                    sleep(0.001)

        threads = [threading.Thread(target=appender), threading.Thread(target=editor)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(60)
        text = self.text("mind_palace:mix.md")
        self.assertIn("counter: 10\n", text)
        for line in appended:
            self.assertIn(line, text)


class LockTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.lock = Path(directory.name) / ".lock"

    def test_the_lock_is_reentrant_in_one_thread(self) -> None:
        with exclusive_file_lock(self.lock):
            with exclusive_file_lock(self.lock):
                pass
            with exclusive_file_lock(self.lock):  # still held by the outer block
                pass

    def test_another_thread_waits_until_the_outer_block_ends(self) -> None:
        order: list[str] = []
        entered = threading.Event()

        def other() -> None:
            with exclusive_file_lock(self.lock):
                order.append("other")

        with exclusive_file_lock(self.lock):
            with exclusive_file_lock(self.lock):
                worker = threading.Thread(target=other)
                worker.start()
                sleep(0.2)
            sleep(0.2)  # the inner block ended: the lock must still be held
            order.append("outer")
            entered.set()
        worker.join(10)
        self.assertEqual(order, ["outer", "other"])

    def test_waiters_are_served_one_at_a_time(self) -> None:
        inside = 0
        peak = 0
        guard = threading.Lock()

        def worker() -> None:
            nonlocal inside, peak
            with exclusive_file_lock(self.lock):
                with guard:
                    inside += 1
                    peak = max(peak, inside)
                sleep(0.01)
                with guard:
                    inside -= 1

        threads = [threading.Thread(target=worker) for _ in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(30)
        self.assertEqual(peak, 1)


class AtomicWriteTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def test_a_failed_write_keeps_the_old_file_and_leaves_no_temporary(self) -> None:
        target = self.root / "note.md"
        target.write_text("old", encoding="utf-8")
        with mock.patch("src.utils.files.os.fsync", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                atomic_replace_bytes(target, b"new")
        self.assertEqual(target.read_text(encoding="utf-8"), "old")
        self.assertEqual([p.name for p in self.root.iterdir()], ["note.md"])

    @unittest.skipIf(os.name == "nt", "POSIX permissions")
    def test_permissions_are_kept_and_new_files_are_not_private(self) -> None:
        target = self.root / "shared.md"
        target.write_text("x", encoding="utf-8")
        target.chmod(0o640)
        atomic_replace_bytes(target, b"y")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)
        fresh = self.root / "fresh.md"
        atomic_replace_bytes(fresh, b"z")
        self.assertNotEqual(stat.S_IMODE(fresh.stat().st_mode), 0o600)

    def test_source_writes_are_atomic(self) -> None:
        sources = SourceRegistry.for_agent("u", "a", data_dir=self.root / "data")
        with mock.patch("src.core.sources.base.atomic_replace_text") as replace:
            sources.write("mind_palace:a.md", "text")
        replace.assert_called_once()


if __name__ == "__main__":
    unittest.main()
