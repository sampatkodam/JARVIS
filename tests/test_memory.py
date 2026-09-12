import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app.db as db
from app.memory import add_message, conversation_history, extract_memories, search_memories, upsert_memory


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
