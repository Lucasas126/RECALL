"""Hybrid search (vectors + BM25 keyword table) and the reserved mind-palace slots of the associative memory."""
from __future__ import annotations

import hashlib
import math
import sqlite3
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from src.agent import associative
from src.config.models import AssociativeMemoryConfig
from src.core import index as index_module
from src.core.index import MANIFEST_FILE, fts_query
from tests.knowledge.test_index import IndexFixture


def _opaque_vector(text: str) -> list[float]:
    """A vector unrelated to the words of the text, so only the keyword table can find a word."""
    digest = hashlib.sha256(text.encode("utf-8")).digest()
    vector = [byte / 255.0 + 0.01 for byte in digest]
    norm = math.sqrt(sum(value * value for value in vector))
    return [value / norm for value in vector]


class OpaqueEmbeddings:
    def __init__(self) -> None:
        self.texts: list[str] = []

    def invoke_embeddings(self, texts: list[str], model: str) -> list[list[float]]:
        self.texts.extend(texts)
        return [_opaque_vector(text) for text in texts]


class HybridIndexTests(IndexFixture):
    def keyword_rows(self) -> dict[str, str]:
        with sqlite3.connect(self.agent_dir / ".index" / MANIFEST_FILE) as db:
            return dict(db.execute("SELECT chunk_id, address FROM chunks_fts").fetchall())

    def test_keyword_table_follows_the_collection(self) -> None:
        self.index.sync()
        self.assertEqual(set(self.keyword_rows()), set(self.all_chunks()))
        (self.agent_dir / "mind_palace" / "pets.md").write_text("# Pets\nMilo is a dog.\n", encoding="utf-8")
        self.index.sync()
        self.assertEqual(set(self.keyword_rows()), set(self.all_chunks()))
        self.chats.delete_chat(self.chat_b)
        self.index.forget_chat(self.chat_b)
        (self.notes / "Garden.md").unlink()
        self.index.sync()
        rows = self.keyword_rows()
        self.assertEqual(set(rows), set(self.all_chunks()))
        self.assertNotIn("notes:Garden.md", rows.values())
        self.assertNotIn(f"conversations:{self.chat_b}.jsonl", rows.values())

    def test_full_reindex_empties_and_refills_the_keyword_table(self) -> None:
        self.index.sync()
        self.index.reindex(full=True)
        self.assertTrue(self.index.wait(30))
        self.assertEqual(set(self.keyword_rows()), set(self.all_chunks()))

    def test_an_index_without_keyword_table_is_backfilled_without_embedding(self) -> None:
        self.index.sync()
        with sqlite3.connect(self.agent_dir / ".index" / MANIFEST_FILE) as db:
            db.execute("DELETE FROM chunks_fts")
            db.execute("DELETE FROM meta WHERE key = 'fts_version'")
        self.provider.reset()
        self.index.sync()
        self.assertEqual(self.provider.texts, [])
        self.assertEqual(set(self.keyword_rows()), set(self.all_chunks()))

    def test_a_rare_word_is_found_by_keyword_when_vectors_miss_it(self) -> None:
        self.provider = OpaqueEmbeddings()
        index = self.make_index(model="fake/opaque")
        for n in range(12):
            self.sources.mind_palace().write(f"filler{n}.md", f"# Filler {n}\nNothing to see in note {n}.\n")
        self.sources.mind_palace().write("gpu.md", "# GPU\nThe card is a model RX7900XTX.\n")
        index.sync()
        hits = index.query("RX7900XTX", 3)
        self.assertEqual(hits[0]["address"], "mind_palace:gpu.md")
        self.assertEqual(hits[0]["source"], "mind_palace")
        expected = sum(a * b for a, b in zip(_opaque_vector("RX7900XTX"), _opaque_vector(hits[0]["document"])))
        self.assertAlmostEqual(hits[0]["score"], expected, places=3)  # the score stays the cosine

    def test_keyword_only_hits_are_scored_by_cosine(self) -> None:
        self.provider = OpaqueEmbeddings()
        index = self.make_index(model="fake/opaque")
        for n in range(6):
            self.sources.mind_palace().write(f"filler{n}.md", f"# Filler {n}\nNothing to see in note {n}.\n")
        self.sources.mind_palace().write("gpu.md", "# GPU\nThe card is a model RX7900XTX.\n")
        index.sync()
        with patch.object(index_module, "_HYBRID_CANDIDATES", 1):
            hits = index.query("RX7900XTX", 1)
        self.assertEqual(len(hits), 1)
        self.assertIsInstance(hits[0]["score"], float)

    def test_filters_apply_to_keyword_matches(self) -> None:
        self.index.sync()
        hits = self.index.query("Luna grey cat", 10, exclude_chat=self.chat_b)
        self.assertNotIn(f"conversations:{self.chat_b}.jsonl", {hit["address"] for hit in hits})
        only = self.index.query("Luna grey cat", 10, only_source="mind_palace")
        self.assertEqual({hit["address"] for hit in only}, {"mind_palace:pets.md"})

    def test_queries_fts_cannot_parse_fall_back_to_vectors(self) -> None:
        self.index.sync()
        self.assertTrue(self.index.query('"" AND OR (*', 5))
        self.assertEqual(fts_query('a "b" NEAR(cd) x-y AND'), '"near" OR "cd" OR "and"')  # operators become plain words
        self.assertEqual(fts_query("Luna luna LUNA cat"), '"luna" OR "cat"')


def _hit(address: str, score: float) -> dict:
    return {"address": address, "source": address.split(":", 1)[0], "document": address, "score": score}


class PalaceSlotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = AssociativeMemoryConfig(
            top_n=5, score_threshold=0.45, palace_slots=2, palace_threshold=0.40, query_model=None,
        )
        self.enterContext(patch.object(associative, "ASSOC", self.config))
        self.found = [
            _hit("conversations:c1.jsonl", 0.70), _hit("conversations:c2.jsonl", 0.65),
            _hit("notes:a.md", 0.60), _hit("conversations:c3.jsonl", 0.55), _hit("conversations:c4.jsonl", 0.50),
            _hit("mind_palace:x.md", 0.43), _hit("mind_palace:reference/old.md", 0.42),
            _hit("mind_palace:y.md", 0.41), _hit("mind_palace:z.md", 0.39),
        ]

    def addresses(self, hits: list[dict]) -> list[str]:
        return [hit["address"] for hit in hits]

    def test_palace_slots_are_filled_even_below_the_general_threshold(self) -> None:
        hits = associative.select_hits(self.found, 5, 2)
        self.assertEqual(self.addresses(hits), [
            "conversations:c1.jsonl", "conversations:c2.jsonl", "notes:a.md",
            "mind_palace:x.md", "mind_palace:reference/old.md",
        ])

    def test_excluded_prefixes_do_not_take_a_slot(self) -> None:
        self.config.palace_exclude = ["reference/"]
        hits = associative.select_hits(self.found, 5, 2)
        self.assertIn("mind_palace:y.md", self.addresses(hits))
        self.assertNotIn("mind_palace:reference/old.md", self.addresses(hits))

    def test_nothing_below_its_threshold_is_injected(self) -> None:
        hits = associative.select_hits(self.found, 9, 9)
        self.assertNotIn("mind_palace:z.md", self.addresses(hits))
        weak = [_hit("conversations:c9.jsonl", 0.44), _hit("mind_palace:w.md", 0.30)]
        self.assertEqual(associative.select_hits(weak, 5, 2), [])

    def test_unused_slots_go_to_the_normal_fill(self) -> None:
        found = [_hit(f"conversations:c{n}.jsonl", 0.9 - n * 0.05) for n in range(6)] + [_hit("mind_palace:x.md", 0.41)]
        hits = associative.select_hits(found, 5, 2)
        self.assertEqual(len(hits), 5)
        self.assertEqual(self.addresses(hits)[-1], "mind_palace:x.md")
        self.assertEqual(self.addresses(hits)[:4], [f"conversations:c{n}.jsonl" for n in range(4)])

    def test_zero_slots_is_plain_hybrid(self) -> None:
        hits = associative.select_hits(self.found, 5, 0)
        self.assertEqual(self.addresses(hits), self.addresses(self.found[:5]))

    def test_slots_never_exceed_the_cap(self) -> None:
        agent = SimpleNamespace(associative_max_matches=1, associative_palace_slots=4)
        self.assertEqual(associative.palace_slots(agent), 1)
        self.assertEqual(len(associative.select_hits(self.found, 1, 4)), 1)
        self.assertEqual(associative.select_hits(self.found, 1, 4)[0]["address"], "mind_palace:x.md")

    def test_user_value_overrides_the_application_default(self) -> None:
        self.assertEqual(associative.palace_slots(SimpleNamespace()), 2)
        self.assertEqual(associative.palace_slots(SimpleNamespace(associative_palace_slots=0)), 0)
        self.assertEqual(associative.palace_slots(SimpleNamespace(associative_palace_slots=3)), 3)

    def test_recall_asks_for_enough_candidates_and_applies_the_slots(self) -> None:
        seen = {}

        def query_memory(query, top_k, *, exclude_chat=None):
            seen["top_k"] = top_k
            return self.found

        agent = SimpleNamespace(config=SimpleNamespace(associative_memory="question_only"), query_memory=query_memory)
        result = associative.recall(agent, "question")
        self.assertGreaterEqual(seen["top_k"], 5)
        self.assertEqual(result[1], 5)
        self.assertIn("mind_palace:x.md", [fragment["address"] for fragment in result.fragments])


if __name__ == "__main__":
    unittest.main()
