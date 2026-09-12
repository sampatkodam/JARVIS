import json
from datetime import datetime, timezone
from threading import Event, Lock, Thread

from app.config import DB_PATH
from app.db import connect
from app.gemini import Gemini

EMBEDDING_MODEL = "gemini-embedding-001"
BATCH_SIZE = 5
POLL_SECONDS = 0.05

_state_lock = Lock()
_state = {"status": "idle", "total": 0, "processed": 0, "embedded": 0, "skipped": 0, "failed": 0, "last_error": None, "started_at": None, "updated_at": None}
_thread = None
_stop = Event()


def _now(): return datetime.now(timezone.utc).isoformat()


def _reset_state(total):
    global _state
    ts = _now()
    with _state_lock:
        _state = {"status": "running", "total": total, "processed": 0, "embedded": 0, "skipped": 0, "failed": 0, "last_error": None, "started_at": ts, "updated_at": ts}


def migration_status():
    with _state_lock:
        state = dict(_state)
    state["remaining"] = max(0, state["total"] - state["processed"])
    state["percent"] = round((state["processed"] / state["total"]) * 100, 1) if state["total"] else 100.0
    return state


def _pending_ids():
    with connect() as conn:
        rows = conn.execute("SELECT id FROM memories WHERE embedding IS NULL OR embedding_model != ? ORDER BY id ASC", (EMBEDDING_MODEL,)).fetchall()
    return [r["id"] for r in rows]


def _fetch_memory(memory_id):
    with connect() as conn:
        return conn.execute("SELECT id,kind,key,content,embedding,embedding_model FROM memories WHERE id=?", (memory_id,)).fetchone()


def _save_embedding(memory_id, values):
    blob = json.dumps([float(v) for v in values], separators=(",", ":")).encode("utf-8")
    with connect() as conn:
        return conn.execute("UPDATE memories SET embedding=?, embedding_model=?, updated_at=? WHERE id=? AND (embedding IS NULL OR embedding_model != ?)", (blob, EMBEDDING_MODEL, _now(), memory_id, EMBEDDING_MODEL)).rowcount


def _run():
    try:
        ids = _pending_ids()
        _reset_state(len(ids))
        gemini = Gemini()
        for memory_id in ids:
            if _stop.is_set():
                with _state_lock: _state["status"] = "stopped"; _state["updated_at"] = _now()
                return
            row = _fetch_memory(memory_id)
            if not row:
                with _state_lock: _state["processed"] += 1; _state["skipped"] += 1; _state["updated_at"] = _now()
                continue
            if row["embedding"] and row["embedding_model"] == EMBEDDING_MODEL:
                with _state_lock: _state["processed"] += 1; _state["skipped"] += 1; _state["updated_at"] = _now()
                continue
            try:
                values = gemini.embed(f"{row['kind']}: {row['key']}\n{row['content']}")
                saved = _save_embedding(memory_id, values) if values else 0
                with _state_lock:
                    _state["processed"] += 1
                    _state["embedded"] += 1 if saved else 0
                    _state["skipped"] += 1 if not saved else 0
                    _state["updated_at"] = _now()
            except Exception as exc:
                with _state_lock:
                    _state["processed"] += 1; _state["failed"] += 1; _state["last_error"] = str(exc)[:500]; _state["updated_at"] = _now()
            _stop.wait(POLL_SECONDS)
        with _state_lock: _state["status"] = "completed"; _state["updated_at"] = _now()
    except Exception as exc:
        with _state_lock: _state["status"] = "failed"; _state["last_error"] = str(exc)[:500]; _state["updated_at"] = _now()


def start_embedding_migration():
    global _thread
    with _state_lock:
        if _thread and _thread.is_alive(): return migration_status()
    _stop.clear()
    _thread = Thread(target=_run, daemon=True, name="jarvis-memory-embedding-migration")
    _thread.start()
    return migration_status()


def stop_embedding_migration():
    _stop.set()
    return migration_status()
