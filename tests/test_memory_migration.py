import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app.db as db
from app.memory import upsert_memory
import app.memory_migration as migration


class FakeGemini:
    calls = 0

    def embed(self, text):
        FakeGemini.calls += 1
        return [1.0, 0.0]


class MemoryMigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(db, "DB_PATH", Path(self.tmp.name) / "jarvis.db")
        self.db_patch.start()
        migration._stop.clear()
        migration._thread = None
        FakeGemini.calls = 0

    def tearDown(self):
        migration._stop.set()
        self.db_patch.stop()
        self.tmp.cleanup()

    def test_migration_skips_existing_embeddings_and_reports_progress(self):
        with patch("app.memory.Gemini", FakeGemini), patch("app.memory_migration.Gemini", FakeGemini):
            existing = upsert_memory("global", None, "fact", "existing", "Already embedded")
            pending = upsert_memory("global", None, "fact", "pending", "Needs embedding")
            # The upsert above creates embeddings via the mocked Gemini, so remove one to simulate a legacy row.
            with db.connect() as conn:
                conn.execute("UPDATE memories SET embedding=NULL, embedding_model=NULL WHERE id=?", (pending,))
            migration._run()
        status = migration.migration_status()
        self.assertEqual(status["status"], "completed")
        self.assertEqual(status["total"], 1)
        self.assertEqual(status["embedded"], 1)
        self.assertEqual(status["failed"], 0)
        self.assertEqual(FakeGemini.calls, 3)  # two upserts + one migration embedding

    def test_start_is_non_blocking_and_idempotent_while_running(self):
        with patch("app.memory_migration._run") as runner:
            first = migration.start_embedding_migration()
            second = migration.start_embedding_migration()
        self.assertIn(first["status"], {"running", "idle"})
        self.assertEqual(second["status"], first["status"])
        self.assertTrue(migration._thread is not None)


if __name__ == "__main__":
    unittest.main()
