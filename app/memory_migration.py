import json
import socket
import uuid
from datetime import datetime, timedelta, timezone
from threading import Event, Lock, Thread

from app.db import connect
from app.gemini import Gemini

EMBEDDING_MODEL = "gemini-embedding-001"
LEASE_SECONDS = 30
POLL_SECONDS = 0.05

_state_lock = Lock()
_state = {"status": "idle", "total": 0, "processed": 0, "embedded": 0, "skipped": 0, "failed": 0, "last_memory_id": 0, "last_error": None, "worker_id": None, "heartbeat_at": None, "started_at": None, "updated_at": None}
_thread = None
_stop = Event()
_WORKER_ID = f"{socket.gethostname()}:{uuid.uuid4()}"


def _now():
    return datetime.now(timezone.utc).isoformat()


def _stale(heartbeat):
    if not heartbeat:
        return True
    try:
        return datetime.fromisoformat(heartbeat) < datetime.now(timezone.utc) - timedelta(seconds=LEASE_SECONDS)
    except ValueError:
        return True


def _db_state():
    with connect() as conn:
        row = conn.execute("SELECT * FROM embedding_migration_jobs WHERE id=1").fetchone()
    return dict(row) if row else None


def migration_status():
    state = _db_state()
    if state is None:
        with _state_lock:
            state = dict(_state)
    else:
        with _state_lock:
            _state.update(state)
    if state.get("status") == "running" and _stale(state.get("heartbeat_at")):
        state["status"] = "stale"
    state["remaining"] = max(0, state["total"] - state["processed"])
    state["percent"] = round((state["processed"] / state["total"]) * 100, 1) if state["total"] else 100.0
    state["resumable"] = state["status"] in {"stopped", "stale", "failed", "idle"}
    return state


def _set_db(updates):
    updates = dict(updates)
    updates["updated_at"] = _now()
    assignments = ", ".join(f"{key}=?" for key in updates)
    values = list(updates.values()) + [1]
    with connect() as conn:
        conn.execute(f"UPDATE embedding_migration_jobs SET {assignments} WHERE id=?", values)
    with _state_lock:
        _state.update(updates)


def _claim_job(reset=False):
    now = _now()
    lease = (datetime.now(timezone.utc) + timedelta(seconds=LEASE_SECONDS)).isoformat()
    with connect() as conn:
        row = conn.execute("SELECT * FROM embedding_migration_jobs WHERE id=1").fetchone()
        if row and row["status"] == "running" and not _stale(row["heartbeat_at"]):
            return row["worker_id"] == _WORKER_ID
        total = conn.execute("SELECT COUNT(*) AS n FROM memories").fetchone()["n"]
        if reset or not row or row["status"] in {"completed", "failed", "idle"}:
            if row:
                conn.execute("UPDATE embedding_migration_jobs SET status='running', total=?, processed=0, embedded=0, skipped=0, failed=0, last_memory_id=0, last_error=NULL, worker_id=?, heartbeat_at=?, started_at=?, updated_at=? WHERE id=1", (total, _WORKER_ID, lease, now, now))
            else:
                conn.execute("INSERT INTO embedding_migration_jobs (id,status,total,processed,embedded,skipped,failed,last_memory_id,last_error,worker_id,heartbeat_at,started_at,updated_at) VALUES (1,'running',?,?,?,?,?,?,?,?,?,?,?)", (total, 0, 0, 0, 0, 0, None, _WORKER_ID, lease, now, now))
        else:
            conn.execute("UPDATE embedding_migration_jobs SET status='running', total=?, worker_id=?, heartbeat_at=?, updated_at=? WHERE id=1", (total, _WORKER_ID, lease, now))
    return True


def _claim_lease():
    lease = (datetime.now(timezone.utc) + timedelta(seconds=LEASE_SECONDS)).isoformat()
    with connect() as conn:
        changed = conn.execute("UPDATE embedding_migration_jobs SET heartbeat_at=?, updated_at=? WHERE id=1 AND status='running' AND worker_id=?", (lease, _now(), _WORKER_ID)).rowcount
    return changed == 1


def _advance(memory_id, **counts):
    lease = (datetime.now(timezone.utc) + timedelta(seconds=LEASE_SECONDS)).isoformat()
    with connect() as conn:
        row = conn.execute("SELECT processed,embedded,skipped,failed FROM embedding_migration_jobs WHERE id=1 AND status='running' AND worker_id=?", (_WORKER_ID,)).fetchone()
        if not row:
            return False
        conn.execute("UPDATE embedding_migration_jobs SET last_memory_id=?, processed=processed+?, embedded=embedded+?, skipped=skipped+?, failed=failed+?, heartbeat_at=?, updated_at=? WHERE id=1 AND status='running' AND worker_id=?", (memory_id, counts.get("processed", 0), counts.get("embedded", 0), counts.get("skipped", 0), counts.get("failed", 0), lease, _now(), _WORKER_ID))
        new_values = {"last_memory_id": memory_id, "processed": row["processed"] + counts.get("processed", 0), "embedded": row["embedded"] + counts.get("embedded", 0), "skipped": row["skipped"] + counts.get("skipped", 0), "failed": row["failed"] + counts.get("failed", 0), "heartbeat_at": lease}
    with _state_lock:
        _state.update(new_values)
    return True


def _memory_after(cursor):
    with connect() as conn:
        return conn.execute("SELECT id,kind,key,content,embedding,embedding_model FROM memories WHERE id>? ORDER BY id ASC", (cursor,)).fetchall()


def _save_embedding(memory_id, values):
    blob = json.dumps([float(v) for v in values], separators=(",", ":")).encode("utf-8")
    with connect() as conn:
        return conn.execute("UPDATE memories SET embedding=?, embedding_model=?, updated_at=? WHERE id=? AND (embedding IS NULL OR embedding_model != ?)", (blob, EMBEDDING_MODEL, _now(), memory_id, EMBEDDING_MODEL)).rowcount


def _run():
    try:
        if not _claim_job():
            return
        gemini = Gemini()
        while True:
            if _stop.is_set():
                _set_db({"status": "stopped", "worker_id": None, "heartbeat_at": None})
                return
            if not _claim_lease():
                return
            state = _db_state()
            rows = _memory_after(int(state.get("last_memory_id", 0)))
            if not rows:
                _set_db({"status": "completed", "worker_id": None, "heartbeat_at": None, "last_error": None})
                return
            for row in rows:
                if _stop.is_set():
                    _set_db({"status": "stopped", "worker_id": None, "heartbeat_at": None})
                    return
                if not _claim_lease():
                    return
                if row["embedding"] and row["embedding_model"] == EMBEDDING_MODEL:
                    _advance(row["id"], processed=1, skipped=1)
                else:
                    try:
                        values = gemini.embed(f"{row['kind']}: {row['key']}\n{row['content']}")
                        saved = _save_embedding(row["id"], values) if values else 0
                        _advance(row["id"], processed=1, embedded=1 if saved else 0, skipped=0 if saved else 1)
                    except Exception as exc:
                        _advance(row["id"], processed=1, failed=1)
                        _set_db({"last_error": str(exc)[:500]})
                _stop.wait(POLL_SECONDS)
    except Exception as exc:
        _set_db({"status": "failed", "worker_id": None, "heartbeat_at": None, "last_error": str(exc)[:500]})


def start_embedding_migration():
    global _thread
    with _state_lock:
        if _thread and _thread.is_alive():
            return migration_status()
    _stop.clear()
    status = migration_status()
    if status["status"] == "failed":
        _claim_job(reset=True)
    _thread = Thread(target=_run, daemon=True, name="jarvis-memory-embedding-migration")
    _thread.start()
    return migration_status()


def stop_embedding_migration():
    _stop.set()
    return migration_status()
