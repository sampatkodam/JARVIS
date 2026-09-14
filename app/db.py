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
    pause_requested INTEGER NOT NULL DEFAULT 0,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
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
CREATE TABLE IF NOT EXISTS task_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT NOT NULL,
    level TEXT NOT NULL DEFAULT 'info',
    message TEXT NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_logs_task_created ON task_logs(task_id, created_at, id);
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    title TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    task_id TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_messages_conversation_created ON messages(conversation_id, created_at, id);
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    scope TEXT NOT NULL,
    scope_id TEXT,
    kind TEXT NOT NULL,
    key TEXT NOT NULL,
    content TEXT NOT NULL,
    source_type TEXT NOT NULL DEFAULT 'manual',
    source_id TEXT,
    confidence REAL NOT NULL DEFAULT 1.0,
    embedding BLOB,
    embedding_model TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    UNIQUE(scope, scope_id, kind, key)
);
CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(scope, scope_id, updated_at);
CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(content, key, kind, scope, scope_id, content='memories', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
    INSERT INTO memory_fts(rowid, content, key, kind, scope, scope_id) VALUES(new.id, new.content, new.key, new.kind, new.scope, new.scope_id);
END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, content, key, kind, scope, scope_id) VALUES('delete', old.id, old.content, old.key, old.kind, old.scope, old.scope_id);
END;
CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
    INSERT INTO memory_fts(memory_fts, rowid, content, key, kind, scope, scope_id) VALUES('delete', old.id, old.content, old.key, old.kind, old.scope, old.scope_id);
    INSERT INTO memory_fts(rowid, content, key, kind, scope, scope_id) VALUES(new.id, new.content, new.key, new.kind, new.scope, new.scope_id);
END;
CREATE TABLE IF NOT EXISTS embedding_migration_jobs (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    status TEXT NOT NULL,
    total INTEGER NOT NULL DEFAULT 0,
    processed INTEGER NOT NULL DEFAULT 0,
    embedded INTEGER NOT NULL DEFAULT 0,
    skipped INTEGER NOT NULL DEFAULT 0,
    failed INTEGER NOT NULL DEFAULT 0,
    last_memory_id INTEGER NOT NULL DEFAULT 0,
    last_error TEXT,
    worker_id TEXT,
    heartbeat_at TEXT,
    started_at TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS tool_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    task_id TEXT,
    step_id INTEGER,
    tool_name TEXT NOT NULL,
    risk_class TEXT NOT NULL,
    decision TEXT NOT NULL,
    reason TEXT NOT NULL,
    arguments TEXT,
    result_summary TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_tool_audit_task_created ON tool_audit(task_id, created_at, id);
CREATE INDEX IF NOT EXISTS idx_tool_audit_decision_created ON tool_audit(decision, created_at, id);
'''


def _prepare(conn):
    conn.executescript(SCHEMA)
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)").fetchall()}
    for name, definition in (("retry_count", "INTEGER NOT NULL DEFAULT 0"), ("next_run_at", "TEXT"), ("worker_heartbeat_at", "TEXT"), ("pause_requested", "INTEGER NOT NULL DEFAULT 0"), ("cancel_requested", "INTEGER NOT NULL DEFAULT 0")):
        if name not in columns:
            conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {definition}")
    memory_columns = {r["name"] for r in conn.execute("PRAGMA table_info(memories)").fetchall()}
    for name, definition in (("embedding", "BLOB"), ("embedding_model", "TEXT")):
        if name not in memory_columns:
            conn.execute(f"ALTER TABLE memories ADD COLUMN {name} {definition}")


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
