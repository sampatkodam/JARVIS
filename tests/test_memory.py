import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

import app.db as db
from app.memory import (
    _cosine,
    _rank_memories,
    add_message,
    conversation_history,
    extract_memories,
    search_memories,
    upsert_memory,
)


class FakeGemini:
    def json(self, prompt, system=""):
        return {"memories": [{"kind": "preference", "key": "editor", "content": "Use a lightweight editor.", "confidence": 0.9}]}

    def embed(self, text):
        text = text.lower()
        return [1.0, 0.0] if "database" in text or "sqlite" in text else [0.0, 1.0]


class MemoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(db, "DB_PATH", Path(self.tmp.name) / "jarvis.db")
        self.db_patch.start()

    def tearDown(self):
        self.db_patch.stop()
        self.tmp.cleanup()

    def test_memory_is_persistent_and_searchable(self):
        with patch("app.memory.Gemini", FakeGemini):
            memory_id = upsert_memory("project", "auri", "decision", "database", "Use SQLite for durable local state.")
            self.assertIsNotNone(memory_id)
            rows = search_memories("durable SQLite state")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["scope"], "project")
        self.assertEqual(rows[0]["scope_id"], "auri")
        self.assertIn("relevance_score", rows[0])
        self.assertIn("semantic_similarity", rows[0])

    def test_semantic_similarity_can_rank_without_exact_keyword_overlap(self):
        with patch("app.memory.Gemini", FakeGemini):
            upsert_memory("global", None, "fact", "storage", "SQLite keeps durable application state.")
            upsert_memory("global", None, "fact", "editor", "Use a lightweight editor for coding.")
            rows = search_memories("database")
        self.assertEqual(rows[0]["key"], "storage")
        self.assertGreater(rows[0]["semantic_similarity"], 0.5)

    def test_confidence_and_scope_are_part_of_ranked_result(self):
        with patch("app.memory.Gemini", FakeGemini):
            upsert_memory("global", None, "fact", "database-global", "SQLite database state.", confidence=0.3)
            upsert_memory("project", "p1", "decision", "database-project", "SQLite database state.", confidence=0.9)
            rows = search_memories("database", scope="project", scope_id="p1")
        self.assertEqual(rows[0]["key"], "database-project")
        self.assertGreater(rows[0]["scope_score"], rows[1]["scope_score"])
        self.assertGreater(rows[0]["relevance_score"], rows[1]["relevance_score"])

    def test_scope_priority_order_is_stable_without_requested_scope(self):
        now = datetime.now(timezone.utc).isoformat()
        rows = [
            {"scope": scope, "scope_id": None, "kind": "fact", "key": scope, "content": "same", "confidence": 1.0, "embedding": None, "updated_at": now}
            for scope in ("global", "conversation", "workspace", "project", "task")
        ]
        with patch("app.memory.Gemini", FakeGemini):
            ranked = _rank_memories("unmatched", rows, limit=10)
        self.assertEqual([row["scope"] for row in ranked], ["task", "project", "workspace", "conversation", "global"])
        self.assertEqual([row["scope_score"] for row in ranked], [1.0, 0.96, 0.9, 0.84, 0.78])

    def test_requested_scope_and_scope_id_receive_exact_priority(self):
        now = datetime.now(timezone.utc).isoformat()
        rows = [
            {"scope": "project", "scope_id": "p1", "kind": "fact", "key": "p1", "content": "same", "confidence": 1.0, "embedding": None, "updated_at": now},
            {"scope": "task", "scope_id": "t1", "kind": "fact", "key": "t1", "content": "same", "confidence": 1.0, "embedding": None, "updated_at": now},
            {"scope": "global", "scope_id": None, "kind": "fact", "key": "global", "content": "same", "confidence": 1.0, "embedding": None, "updated_at": now},
        ]
        with patch("app.memory.Gemini", FakeGemini):
            ranked = _rank_memories("unmatched", rows, limit=10, scope="project", scope_id="p1")
        self.assertEqual(ranked[0]["scope"], "project")
        self.assertEqual(ranked[0]["scope_score"], 1.0)

    def test_unembedded_memory_uses_lexical_fallback(self):
        now = datetime.now(timezone.utc).isoformat()
        rows = [{
            "scope": "global", "scope_id": None, "kind": "fact", "key": "database",
            "content": "SQLite is the durable database.", "confidence": 1.0,
            "embedding": None, "updated_at": now,
        }]
        with patch("app.memory.Gemini", FakeGemini):
            ranked = _rank_memories("database", rows, limit=1)
        self.assertEqual(ranked[0]["lexical_score"], 1.0)
        self.assertEqual(ranked[0]["semantic_similarity"], 1.0)

    def test_unrelated_embeddings_do_not_get_artificial_similarity(self):
        self.assertEqual(_cosine([1.0, 0.0], [0.0, 1.0]), 0.0)
        self.assertEqual(_cosine([1.0, 0.0], [-1.0, 0.0]), 0.0)

    def test_lexical_overlap_boosts_embedded_similarity_without_baseline(self):
        now = datetime.now(timezone.utc).isoformat()
        rows = [{
            "scope": "global", "scope_id": None, "kind": "fact", "key": "database",
            "content": "Unrelated wording.", "confidence": 1.0,
            "embedding": '[0.6,0.8]'.encode("utf-8"), "updated_at": now,
        }]
        with patch("app.memory.Gemini", FakeGemini):
            ranked = _rank_memories("database", rows, limit=1)
        self.assertEqual(ranked[0]["lexical_score"], 1.0)
        self.assertEqual(ranked[0]["semantic_similarity"], 1.0)

    def test_conversation_history_round_trips(self):
        add_message("conv-1", "user", "Remember that the project uses SQLite.")
        add_message("conv-1", "assistant", "I will use that context.")
        history = conversation_history("conv-1")
        self.assertEqual([m["role"] for m in history], ["user", "assistant"])
        self.assertIn("SQLite", history[0]["content"])

    def test_gemini_extraction_persists_supported_memory(self):
        with patch("app.memory.Gemini", FakeGemini):
            ids = extract_memories("The user prefers a lightweight editor.", scope="global", source_id="conv-1")
        self.assertEqual(len(ids), 1)
        rows = search_memories("lightweight editor")
        self.assertEqual(rows[0]["key"], "editor")
        self.assertEqual(rows[0]["source_type"], "conversation")


if __name__ == "__main__":
    unittest.main()
