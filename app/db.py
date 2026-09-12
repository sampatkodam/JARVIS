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

@contextmanager
def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        conn.executescript(SCHEMA)
        yield conn
        conn.commit()
    finally:
        conn.close()
