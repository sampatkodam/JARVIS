import tempfile
import threading
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import app.db as db
from app.db import connect
from app.queue import claim_next_task, mark_retry


class QueueHardeningTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "jarvis-test.db"
        self.db_patch = patch.object(db, "DB_PATH", self.db_path)
        self.db_patch.start()
        with connect() as conn:
            conn.execute(
                """INSERT INTO tasks(id, goal, status, created_at, updated_at)
                   VALUES(?,?,?,?,?)""",
                (str(uuid.uuid4()), "test", "waiting", self.ts(), self.ts()),
            )
        self.task_id = self._task_id()

    def tearDown(self):
        self.db_patch.stop()
        self.tmp.cleanup()

    @staticmethod
    def ts(value=None):
        return (value or datetime.now(timezone.utc)).isoformat()

    def _task_id(self):
        with connect() as conn:
            return conn.execute("SELECT id FROM tasks ORDER BY created_at LIMIT 1").fetchone()[0]

    def test_multiple_workers_can_claim_a_task_only_once(self):
        barrier = threading.Barrier(2)
        results = []
        lock = threading.Lock()

        def worker():
            barrier.wait()
            result = claim_next_task()
            with lock:
                results.append(result)

        threads = [threading.Thread(target=worker) for _ in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=5)

        self.assertEqual(len(results), 2)
        self.assertEqual(results.count(self.task_id), 1)
        self.assertEqual(results.count(None), 1)
        with connect() as conn:
            status = conn.execute("SELECT status FROM tasks WHERE id=?", (self.task_id,)).fetchone()[0]
        self.assertEqual(status, "running")

    def test_retry_uses_utc_iso_timestamp_and_waits_for_backoff(self):
        with connect() as conn:
            conn.execute(
                "UPDATE tasks SET status='running', retry_count=0, next_run_at=NULL WHERE id=?",
                (self.task_id,),
            )

        before = datetime.now(timezone.utc)
        mark_retry(self.task_id, "temporary failure")
        after = datetime.now(timezone.utc)

        with connect() as conn:
            row = conn.execute(
                "SELECT status, retry_count, next_run_at FROM tasks WHERE id=?", (self.task_id,)
            ).fetchone()
        scheduled = datetime.fromisoformat(row["next_run_at"])

        self.assertEqual(row["status"], "retrying")
        self.assertEqual(row["retry_count"], 1)
        self.assertIsNotNone(scheduled.tzinfo)
        self.assertEqual(scheduled.utcoffset(), timedelta(0))
        self.assertGreaterEqual(scheduled, before + timedelta(seconds=1.9))
        self.assertLessEqual(scheduled, after + timedelta(seconds=2.2))
        self.assertIsNone(claim_next_task(), "retrying task must not be claimed before its due time")

        time.sleep(2.2)
        self.assertEqual(claim_next_task(), self.task_id)


if __name__ == "__main__":
    unittest.main()
