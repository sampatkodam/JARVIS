import sqlite3
from contextlib import contextmanager
from app.config import DB_PATH

SCHEMA = '''
CREATE TABLE IF NOT EXISTS tasks (
    id TEXT PRIMARY KEY,
    goal TEXT NOT NULL,
    status TEXT NOT NULL,
    result TEXT,
    error TEXT,
    retry_count INTEGER NOT NULL DEFAULT 0,
    next_run_at TEXT,
    worker_heartbeat_at TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    step_no INTEGER NOT NULL,
    action TEXT NOT NULL,
    description TEXT NOT NULL,
    status TEXT NOT NULL,
    tool_name TEXT,
    tool_args TEXT,
    output TEXT,
    critique TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(task_id, step_no)
);
'''


def _prepare(conn):
    conn.executescript(SCHEMA)
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
    for name, definition in (
        ("retry_count", "INTEGER NOT NULL DEFAULT 0"),
        ("next_run_at", "TEXT"),
        ("worker_heartbeat_at", "TEXT"),
    ):
        if name not in columns:
            conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {definition}")


@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        _prepare(conn)
        yield conn
        conn.commit()
    finally:
        conn.close()
