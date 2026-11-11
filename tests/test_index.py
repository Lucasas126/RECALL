from __future__ import annotations

import os
import subprocess
import sys
import textwrap
import time
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
from unittest.mock import patch

import chromadb

from index_helpers import FakeEmbeddings
from src.agent import associative
from src.agent.toolsets.editing import build_edit_tools
from src.agent.toolsets.memory import build_memory_tools
from src.config.models import FolderConfig, IndexConfig
from src.core.chunking import (
    ChunkSizes, chunk_exchange, chunk_markdown, chunk_skill, estimate_tokens, excerpt,
)
from src.core.index import VectorIndex, close_index
from src.core.sources.registry import SourceRegistry

SETTINGS = IndexConfig(chunk_tokens=120, max_chunk_tokens=200, overlap_tokens=20, folders_reconcile_minutes=10)
SKILL = "---\nname: weekly-plan\ndescription: Plan the user's week from the calendar\n---\nIntro line.\n\n## Steps\n1. Read the calendar.\n2. Draft.\n"


class ChunkingTests(unittest.TestCase):
    def test_heading_path_and_title_prefix(self) -> None:
        text = "# Garden\nIntro.\n\n## Roses\nPrune in March.\n\n### Pests\nAphids hate garlic.\n\n## Trees\nWater weekly.\n"
        chunks = chunk_markdown(text, "notes:home/Garden.md")
        self.assertEqual([chunk.text.split("\n\n")[0] for chunk in chunks], [
            "notes:home/Garden.md > Garden",
            "notes:home/Garden.md > Garden > Roses",
            "notes:home/Garden.md > Garden > Roses > Pests",
            "notes:home/Garden.md > Garden > Trees",
        ])
        self.assertEqual(chunks[2].context, "Garden > Roses > Pests")
        self.assertEqual(chunks[2].body, "Aphids hate garlic.")
        # without a matching first heading the file name is the title
        untitled = chunk_markdown("## Recipe\nMix.", "mind_palace:cooking.md")
        self.assertEqual(untitled[0].text.split("\n\n")[0], "mind_palace:cooking.md > cooking > Recipe")

    def test_packing_respects_sizes_and_overlaps_within_a_section(self) -> None:
        sizes = ChunkSizes(target=50, maximum=80, overlap=10)
        paragraphs = [f"Paragraph {n} talks about topic {n}. It has a second sentence here." for n in range(12)]
        chunks = chunk_markdown("# Note\n" + "\n\n".join(paragraphs), "notes:Note.md", sizes)
        self.assertGreater(len(chunks), 2)
        for chunk in chunks:
            self.assertLessEqual(estimate_tokens(chunk.body), sizes.maximum)
        for previous, current in zip(chunks, chunks[1:]):
            tail = previous.body.split("\n\n")[-1]
            overlap = current.body.split("\n\n")[0]
            self.assertTrue(tail.endswith(overlap), (tail, overlap))  # the next chunk repeats the end of the previous
        # every paragraph is in some chunk
        joined = "\n".join(chunk.body for chunk in chunks)
        for paragraph in paragraphs:
            self.assertIn(paragraph, joined)

    def test_code_blocks_are_not_split_and_hide_headings(self) -> None:
        code = "```python\n# not a heading\n" + "\n".join(f"value_{n} = {n}" for n in range(30)) + "\n```"
        text = f"# Script\nSome intro text.\n\n{code}\n\nAfter the code."
        chunks = chunk_markdown(text, "notes:script.md", ChunkSizes(target=40, maximum=400, overlap=10))
        self.assertTrue(any(code in chunk.body for chunk in chunks), "the code block stays whole")
        self.assertFalse(any("not a heading" in chunk.context for chunk in chunks))
        # a block over the maximum is split by lines, each piece still fenced
        huge = chunk_markdown(f"# Script\n{code}", "notes:script.md", ChunkSizes(target=30, maximum=40, overlap=0))
        self.assertGreater(len(huge), 1)
        for chunk in huge:
            self.assertTrue(chunk.body.startswith("```python") and chunk.body.endswith("```"), chunk.body)

    def test_skill_sections_are_prefixed_with_name_and_description(self) -> None:
        chunks = chunk_skill(SKILL, "agent:skills/weekly-plan/SKILL.md")
        self.assertEqual(len(chunks), 2)
        self.assertTrue(chunks[1].text.startswith(
            "agent:skills/weekly-plan/SKILL.md > skill weekly-plan: Plan the user's week from the calendar > Steps\n\n"
        ))
        self.assertNotIn("description:", chunks[0].body)

    def test_exchange_is_one_chunk_unless_very_long(self) -> None:
        short = chunk_exchange("conversations:c1 > Trip (2026-11-03)", "Where?", "To Lisbon.")
        self.assertEqual(short[0].text, "conversations:c1 > Trip (2026-11-03)\n\nuser: Where?\n\nassistant: To Lisbon.")
        long_reply = "\n\n".join(f"Point {n}: " + "detail " * 40 for n in range(10))
        parts = chunk_exchange("conversations:c1", "Explain", long_reply, ChunkSizes(100, 150, 10))
        self.assertGreater(len(parts), 1)
        self.assertTrue(all(part.text.startswith("conversations:c1\n\n") for part in parts))

    def test_excerpt_cuts_at_a_sentence_end(self) -> None:
        text = "First sentence is here. " * 30
        cut = excerpt(text, 400)
        self.assertLessEqual(len(cut), 400)
        self.assertTrue(cut.endswith("here."))
        words = excerpt("word " * 200, 50)
        self.assertTrue(words.endswith("…") and len(words) <= 51)
        self.assertEqual(associative._fragment("Short."), "Short.")


class IndexFixture(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory(ignore_cleanup_errors=True)
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.data = self.root / "data"
        self.agent_dir = self.data / "u" / "a"
        self.notes = self.root / "notes"
        self.notes.mkdir()
        (self.notes / "Garden.md").write_text("# Garden\nRoses need pruning in March.\n", encoding="utf-8")
        (self.notes / ".obsidian").mkdir()
        (self.notes / ".obsidian" / "workspace.md").write_text("hidden settings", encoding="utf-8")
        self.provider = FakeEmbeddings()
        self.folders = [FolderConfig(name="notes", path=str(self.notes))]
        self.sources = self.registry()
        palace = self.sources.mind_palace()
        palace.write("pets.md", "# Pets\nLuna is a grey cat who loves tuna.\n")
        files = self.sources.agent_files()
        files.write_soul("I am Aria.")
        files.write_memory_text("memory", "The user likes tea.")
        files.write_skill("weekly-plan", SKILL)
        self.chats = self.sources.conversations()
        self.chat_a = self.chats.create()
        self._exchange(self.chat_a, "Where should we travel?", "Lisbon has great food.")
        self.chat_b = self.chats.create()
        self._exchange(self.chat_b, "Tell me about my cat", "Luna is your grey cat.")
        self.index = self.make_index()

    def registry(self, folders: list[FolderConfig] | None = None) -> SourceRegistry:
        return SourceRegistry.for_agent(
            "u", "a", data_dir=self.data, folders=self.folders if folders is None else folders,
            folders_readable=True,
        )

    def make_index(self, model: str = "fake/embed", settings: IndexConfig = SETTINGS, sources: SourceRegistry | None = None):
        index = VectorIndex(self.agent_dir / ".index", sources or self.sources, self.provider, model, settings)
        self.addCleanup(close_index, self.agent_dir / ".index")
        return index

    def _exchange(self, chat_id: str, user: str, reply: str) -> None:
        self.chats.append(chat_id, "user", user)
        self.chats.append(chat_id, "assistant", reply)

    def all_chunks(self) -> dict[str, dict]:
        client = chromadb.PersistentClient(path=str((self.agent_dir / ".index").resolve()))
        found = client.get_collection("memory").get(include=["metadatas", "documents"])
        return {doc_id: {**meta, "document": doc} for doc_id, meta, doc in zip(found["ids"], found["metadatas"], found["documents"])}


class IndexTests(IndexFixture):
    def test_sources_indexed_and_excluded(self) -> None:
        self.index.sync()
        addresses = {meta["address"] for meta in self.all_chunks().values()}
        self.assertEqual(addresses, {
            "mind_palace:pets.md", "agent:skills/weekly-plan/SKILL.md", "notes:Garden.md",
            f"conversations:{self.chat_a}.jsonl", f"conversations:{self.chat_b}.jsonl",
        })  # SOUL.md, MEMORY.md and hidden folder entries are not indexed
        status = self.index.status()
        self.assertEqual(status["chats"], 2)
        self.assertEqual(status["files"], 3)
        self.assertFalse(status["rebuilding"])
        self.assertIsNotNone(status["last_reconcile"])
        self.assertIn("notes", status["sources"])

    def test_new_turns_only_are_embedded(self) -> None:
        self.index.sync()
        self.provider.reset()
        self.chats.append(self.chat_a, "user", "What about Porto?")
        self.index.after_turn(self.chat_a)
        self.assertTrue(self.index.wait(10))
        self.assertEqual(self.provider.texts, [], "a turn still waiting for its reply is not indexed")
        self.chats.append(self.chat_a, "assistant", "Porto is lovely in spring.")
        self.index.after_turn(self.chat_a)
        self.assertTrue(self.index.wait(10))
        self.assertEqual(len(self.provider.texts), 1)
        embedded = self.provider.texts[0]
        self.assertIn("user: What about Porto?", embedded)
        self.assertIn("assistant: Porto is lovely in spring.", embedded)
        self.assertNotIn("Lisbon", embedded)
        self.assertTrue(embedded.startswith(f"conversations:{self.chat_a} > Where should we travel? ("))
        ids = [doc_id for doc_id in self.all_chunks() if doc_id.startswith(f"conversations:{self.chat_a}::")]
        self.assertEqual(sorted(ids), [f"conversations:{self.chat_a}::turn0", f"conversations:{self.chat_a}::turn2"])
        # a reconcile afterwards has nothing left to do
        self.provider.reset()
        self.index.sync()
        self.assertEqual(self.provider.texts, [])

    def test_current_chat_is_excluded_from_results(self) -> None:
        self.index.sync()
        hits = self.index.query("grey cat Luna", 10)
        self.assertIn(f"conversations:{self.chat_b}.jsonl", {hit["address"] for hit in hits})
        hits = self.index.query("grey cat Luna", 10, exclude_chat=self.chat_b)
        self.assertNotIn(f"conversations:{self.chat_b}.jsonl", {hit["address"] for hit in hits})
        self.assertIn("mind_palace:pets.md", {hit["address"] for hit in hits})
        pets = next(hit for hit in hits if hit["address"] == "mind_palace:pets.md")
        self.assertEqual(pets["context"], "Pets")
        self.assertEqual(pets["body"], "Luna is a grey cat who loves tuna.")

    def test_recall_passes_the_chat_being_answered(self) -> None:
        seen = {}

        def query_memory(query, top_k, *, exclude_chat=None):
            seen.update(query=query, exclude_chat=exclude_chat)
            return [{"address": "mind_palace:pets.md", "context": "Pets", "body": "Luna. " * 100, "document": "", "score": 0.9}]

        agent = SimpleNamespace(config=SimpleNamespace(associative_memory="question_only"), query_memory=query_memory)
        hint, count = associative.recall(agent, "my cat", chat_id="chat-1")
        self.assertEqual(seen, {"query": "my cat", "exclude_chat": "chat-1"})
        self.assertEqual(count, 1)
        line = hint.splitlines()[-1]
        self.assertTrue(line.startswith("- [mind_palace:pets.md > Pets] Luna."))
        self.assertLessEqual(len(line), len("- [mind_palace:pets.md > Pets] ") + 400)

    def test_recall_returns_structured_hits(self) -> None:
        hits = [
            {"address": "mind_palace:pets.md", "context": "Pets", "body": "Luna. " * 100, "document": "", "score": 0.9},
            {"address": "notes:misc.md", "context": "", "document": "Short note.", "score": 0.8},
        ]
        agent = SimpleNamespace(
            config=SimpleNamespace(associative_memory="question_only"),
            query_memory=lambda query, top_k, *, exclude_chat=None: hits,
        )
        result = associative.recall(agent, "my cat")
        hint, count = result
        self.assertEqual(count, 2)
        self.assertIsNone(result.query)  # the user's own message is not a proposed query
        pets, misc = result.fragments
        self.assertEqual((pets["address"], pets["label"], pets["score"]), ("mind_palace:pets.md", "Pets", 0.9))
        self.assertTrue(pets["excerpt"].startswith("Luna."))
        self.assertLessEqual(len(pets["excerpt"]), associative._FRAGMENT_LIMIT)
        self.assertEqual(misc, {"address": "notes:misc.md", "label": None, "score": 0.8, "excerpt": "Short note."})
        self.assertIn("- [mind_palace:pets.md > Pets] Luna.", hint)

    def test_recall_keeps_the_proposed_query_without_hits(self) -> None:
        agent = SimpleNamespace(
            config=SimpleNamespace(associative_memory="llm"),
            query_memory=lambda query, top_k, *, exclude_chat=None: [],
        )
        with patch("src.agent.associative._propose_query", return_value="cat name"):
            result = associative.recall(agent, "what is my cat called?")
        self.assertEqual(tuple(result), (None, 0))
        self.assertEqual((result.fragments, result.query), ([], "cat name"))

    def test_unchanged_files_are_never_read(self) -> None:
        self.index.sync()
        self.provider.reset()
        real_open, real_read = Path.open, Path.read_bytes

        def guard(real):
            def guarded(path, *args, **kwargs):
                if path.suffix in (".md", ".txt", ".jsonl"):
                    raise AssertionError(f"unchanged file was read: {path}")
                return real(path, *args, **kwargs)
            return guarded

        with patch.object(Path, "read_bytes", guard(real_read)), patch.object(Path, "open", guard(real_open)):
            self.index.sync()
        self.assertEqual(self.provider.texts, [])
        # same content with a new mtime: read and hashed, but not embedded again
        pets = self.agent_dir / "mind_palace" / "pets.md"
        os.utime(pets, ns=(time.time_ns(), time.time_ns() + 5_000_000_000))
        self.index.sync()
        self.assertEqual(self.provider.texts, [])
        pets.write_text("# Pets\nLuna is a grey cat who loves salmon.\n", encoding="utf-8")
        self.index.sync()
        self.assertEqual(len(self.provider.texts), 1)
        self.assertIn("salmon", self.provider.texts[0])

    def test_deletions_are_reconciled(self) -> None:
        self.index.sync()
        (self.agent_dir / "mind_palace" / "pets.md").unlink()
        (self.notes / "Garden.md").unlink()
        self.chats.delete_chat(self.chat_b)
        self.index.sync()
        addresses = {meta["address"] for meta in self.all_chunks().values()}
        self.assertNotIn("mind_palace:pets.md", addresses)
        self.assertNotIn("notes:Garden.md", addresses)
        self.assertNotIn(f"conversations:{self.chat_b}.jsonl", addresses)
        self.assertEqual(self.index.status()["chats"], 1)

    def test_forget_chat_drops_its_chunks_at_once(self) -> None:
        self.index.sync()
        self.index.forget_chat(self.chat_b)
        addresses = {meta["address"] for meta in self.all_chunks().values()}
        self.assertNotIn(f"conversations:{self.chat_b}.jsonl", addresses)
        self.assertIn(f"conversations:{self.chat_a}.jsonl", addresses)
        self.assertEqual(self.index.status()["chats"], 1)

    def test_missing_folder_is_skipped_not_fatal(self) -> None:
        missing = FolderConfig(name="offline", path=str(self.root / "not-mounted"))
        with self.assertLogs("recall.sources", level="WARNING"):
            sources = self.registry(folders=[*self.folders, missing])
        self.assertIn("notes", sources.names())
        self.assertNotIn("offline", sources.names())

    def test_query_memory_tool_excludes_the_current_chat(self) -> None:
        seen = {}

        def query_memory(query, top_k, *, exclude_chat=None):
            seen["exclude_chat"] = exclude_chat
            return []

        host = SimpleNamespace(sources=self.sources, query_memory=query_memory, chat_id="chat-7")
        tools = {func.tool_spec["function"]["name"]: func for func in build_memory_tools(host)}
        tools["query_memory"](query="cat")
        self.assertEqual(seen, {"exclude_chat": "chat-7"})

    def test_tool_writes_are_indexed_after_the_turn(self) -> None:
        self.index.sync()
        self.provider.reset()
        written: list[str] = []
        host = SimpleNamespace(sources=self.sources, note_written=written.append)
        tools = {func.tool_spec["function"]["name"]: func for func in build_edit_tools(host)}
        tools["write"](memory_path="mind_palace:trips.md", content="# Trips\nPorto in May.\n")
        tools["delete"](memory_path="mind_palace:pets.md")
        self.assertEqual(written, ["mind_palace:trips.md", "mind_palace:pets.md"])
        self.index.after_turn(self.chat_a, written)
        self.assertTrue(self.index.wait(10))
        addresses = {meta["address"] for meta in self.all_chunks().values()}
        self.assertIn("mind_palace:trips.md", addresses)
        self.assertNotIn("mind_palace:pets.md", addresses)
        self.assertEqual(len(self.provider.texts), 1)

    def test_identity_change_rebuilds_the_index(self) -> None:
        self.index.sync()
        before = len(self.all_chunks())
        other = self.make_index(model="fake/other")
        self.assertEqual(other.query("cat", 5), [], "vectors of another model are never searched")
        self.assertTrue(other.status()["rebuilding"])
        self.provider.reset()
        other.reconcile()
        self.assertTrue(other.wait(20))
        self.assertEqual({model for model, _ in self.provider.calls}, {"fake/other"})
        self.assertEqual(len(self.all_chunks()), before)
        metadata = chromadb.PersistentClient(path=str((self.agent_dir / ".index").resolve())).get_collection("memory").metadata
        self.assertEqual(metadata["embeddings_model"], "fake/other")
        self.assertEqual(metadata["chunking_version"], "2:120/200/20")
        self.assertFalse(other.status()["rebuilding"])
        self.assertTrue(other.query("grey cat", 3))
        # changing the chunk sizes changes the identity too
        resized = self.make_index(model="fake/other", settings=SETTINGS.model_copy(update={"chunk_tokens": 100}))
        self.assertTrue(resized.status()["rebuilding"])

    def test_an_index_from_the_previous_version_is_rebuilt(self) -> None:
        close_index(self.agent_dir / ".index")
        old = chromadb.PersistentClient(path=str((self.agent_dir / ".index").resolve()))
        old.get_or_create_collection("memory", metadata={"hnsw:space": "cosine"}).upsert(
            ids=["old"], documents=["user: old"], embeddings=[[1.0] * 8], metadatas=[{"address": "x", "md5": "y"}],
        )
        from src.core.index import _drop_chroma_system
        _drop_chroma_system(str((self.agent_dir / ".index").resolve()))
        index = self.make_index()
        self.assertEqual(index.query("old", 3), [])
        index.sync()
        self.assertNotIn("old", self.all_chunks())
        self.assertTrue(index.query("grey cat", 3))

    def test_folders_reconcile_only_when_due(self) -> None:
        self.index.sync()
        self.provider.reset()
        (self.notes / "Trees.md").write_text("# Trees\nWater weekly.\n", encoding="utf-8")
        self.index.reconcile()  # folders were scanned moments ago: only the agent's own sources
        self.assertTrue(self.index.wait(10))
        self.assertEqual(self.provider.texts, [])
        self.index.reconcile(folders=True)  # on demand (the web UI's reindex)
        self.assertTrue(self.index.wait(10))
        self.assertEqual(len(self.provider.texts), 1)
        not_indexed = self.registry([FolderConfig(name="notes", path=str(self.notes), indexed=False)])
        self.assertNotIn("notes", [source.name for source in not_indexed.indexed_sources()])

    def test_reindex_full_embeds_everything_again(self) -> None:
        self.index.sync()
        total = len(self.all_chunks())
        self.provider.reset()
        self.index.reindex(full=True)
        self.assertTrue(self.index.wait(20))
        self.assertEqual(len(self.provider.texts), total)
        self.assertFalse(self.index.status()["rebuilding"])


class CrossProcessTests(IndexFixture):
    def test_writes_from_another_process_are_seen(self) -> None:
        self.index.sync()
        self.assertTrue(self.index.query("grey cat", 3))  # this process holds an open Chroma client
        script = textwrap.dedent(f"""
            import sys
            sys.path[:0] = [{str(Path(__file__).parent)!r}, {str(Path(__file__).parents[1])!r}]
            from pathlib import Path
            from index_helpers import FakeEmbeddings
            from src.config.models import FolderConfig, IndexConfig
            from src.core.index import VectorIndex, close_index
            from src.core.sources.registry import SourceRegistry
            settings = IndexConfig.model_validate_json({SETTINGS.model_dump_json()!r})
            sources = SourceRegistry.for_agent("u", "a", data_dir=Path({str(self.data)!r}))
            sources.mind_palace().write("boats.md", "# Boats\\nThe sailboat is called Brisa.\\n")
            index = VectorIndex(Path({str(self.agent_dir / '.index')!r}), sources, FakeEmbeddings(), "fake/embed", settings)
            index.sync()
            close_index(index.path)
        """)
        result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr[-3000:])
        hits = self.index.query("sailboat Brisa", 3)
        self.assertEqual(hits[0]["address"], "mind_palace:boats.md")
        # and this process's next write is seen by a fresh client too
        self.sources.mind_palace().write("kites.md", "# Kites\nRed kite.\n")
        self.index.sync()
        self.assertIn("mind_palace:kites.md", {meta["address"] for meta in self.all_chunks().values()})


if __name__ == "__main__":
    unittest.main()
