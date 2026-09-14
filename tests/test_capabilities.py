import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import app.capabilities as capabilities
import app.db as db
from app.tools.registry import execute_tool


class CapabilityTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.tmp.name) / "workspace"
        self.workspace.mkdir()
        self.db_patch = patch.object(db, "DB_PATH", Path(self.tmp.name) / "jarvis.db")
        self.workspace_patch = patch.object(capabilities, "WORKSPACE", self.workspace)
        self.db_patch.start()
        self.workspace_patch.start()

    def tearDown(self):
        self.workspace_patch.stop()
        self.db_patch.stop()
        self.tmp.cleanup()

    def test_workspace_write_allowed(self):
        allowed, reason, cap = capabilities.evaluate("write_file", {"path": "src/app.py"})
        self.assertTrue(allowed, reason)
        self.assertEqual(cap.risk_class, capabilities.WORKSPACE_MUTATION)

    def test_parent_traversal_rejected(self):
        allowed, reason, _ = capabilities.evaluate("write_file", {"path": "../outside.txt"})
        self.assertFalse(allowed)
        self.assertIn("traversal", reason)

    def test_unknown_executable_rejected(self):
        allowed, _, cap = capabilities.evaluate("run_shell", {"command": "unknown_tool --check"})
        self.assertFalse(allowed)
        self.assertEqual(cap.risk_class, capabilities.SYSTEM_LEVEL)

    def test_development_commands_allowed(self):
        for command in ("python -m unittest discover -s tests -v", "npm install", "npm run build", "git status --short"):
            with self.subTest(command=command):
                allowed, reason, _ = capabilities.evaluate("run_shell", {"command": command})
                self.assertTrue(allowed, reason)

    def test_chained_unknown_executable_rejected(self):
        allowed, _, _ = capabilities.evaluate("run_shell", {"command": "python -m unittest && unknown_tool --check"})
        self.assertFalse(allowed)

    def test_audit_row_written_for_rejection(self):
        result = execute_tool("run_shell", {"command": "unknown_tool --check"}, task_id="task-1", step_id=3)
        self.assertFalse(result["success"])
        with db.connect() as conn:
            row = conn.execute("SELECT tool_name, decision, risk_class, task_id, step_id FROM tool_audit ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(row["tool_name"], "run_shell")
        self.assertEqual(row["decision"], "denied")
        self.assertEqual(row["risk_class"], capabilities.SYSTEM_LEVEL)
        self.assertEqual(row["task_id"], "task-1")
        self.assertEqual(row["step_id"], 3)


if __name__ == "__main__":
    unittest.main()
