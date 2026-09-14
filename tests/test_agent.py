import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app.db as db
from app.agent import _recover_interrupted_steps, create_task, get_task


class AgentRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_patch = patch.object(db, "DB_PATH", Path(self.tmp.name) / "jarvis.db")
        self.db_patch.start()

    def tearDown(self):
        self.db_patch.stop()
        self.tmp.cleanup()

    def test_inflight_step_is_recovered_as_interrupted(self):
        task_id = create_task("Recover an interrupted autonomous task")
        with db.connect() as conn:
            conn.execute(
                """UPDATE tasks SET status='running' WHERE id=?""",
                (task_id,),
            )
            conn.execute(
                """INSERT INTO steps
                (task_id,step_no,action,description,status,tool_name,tool_args,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (task_id, 1, "shell", "Run a side-effecting command", "running", "shell", "{}", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
            )

        recovered = _recover_interrupted_steps(task_id)
        record = get_task(task_id)

        self.assertEqual(len(recovered), 1)
        self.assertEqual(record["steps"][0]["status"], "interrupted")
        self.assertIn("outcome is unknown", record["steps"][0]["critique"])

    def test_recovery_is_idempotent(self):
        task_id = create_task("Recover exactly once")
        with db.connect() as conn:
            conn.execute("UPDATE tasks SET status='running' WHERE id=?", (task_id,))
            conn.execute(
                """INSERT INTO steps
                (task_id,step_no,action,description,status,tool_name,tool_args,created_at,updated_at)
                VALUES(?,?,?,?,?,?,?,?,?)""",
                (task_id, 1, "reason", "Interrupted planning step", "running", "reason", "{}", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"),
            )

        self.assertEqual(len(_recover_interrupted_steps(task_id)), 1)
        self.assertEqual(_recover_interrupted_steps(task_id), [])


if __name__ == "__main__":
    unittest.main()
