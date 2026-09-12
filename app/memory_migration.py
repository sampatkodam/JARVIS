import json
from datetime import datetime, timezone
from threading import Event, Lock, Thread

from app.db import connect
from app.gemini import Gemini

EMBEDDING_MODEL = "gemini-embedding-001"
POLL_SECONDS = 0.05

_state_lock = Lock()
_state = {"status": "idle", "total": 0, "processed": 0, "embedded": 0, "skipped": 0, "failed": 0, "last_error": None, "started_at": None, "updated_at": None}
_thread = None
_stop = Event()


def _now(): return datetime.now(timezone.utc).isoformat()


def _set_status(status=None, **updates):
    with _state_lock:
        if status is not None: _state["status"] = status
        _state.update(updates)
        _state["updated_at"] = _now()


def _reset_state(total):
    ts = _now()
    with _state_lock:
        _state = {"status": "running", "total": total, "processed": 0, "embedded": 0, "skipped": 0, "failed": 0, "last_error": None, "started_at": ts, "updated_at": ts}
        globals()["_state"] = _state


def migration_status():
    with _state_lock:
        state = dict(_state)
    state["remaining"] = max(0, state["total"] - state["processed"])
    state["percent"] = round((state["processed"] / state["total"]) * 100, 1) if state["total"] else 100.0
    return state


def _memory_ids():
    with connect() as conn:
        rows = conn.execute("SELECT id FROM memories ORDER BY id ASC").fetchall()
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
        ids = _memory_ids()
        _reset_state(len(ids))
        if not ids:
            _set_status("completed")
            return
        gemini = Gemini()
        for memory_id in ids:
            if _stop.is_set():
                _set_status("stopped")
                return
            row = _fetch_memory(memory_id)
            if not row:
                _set_status(processed=_state["processed"] + 1, skipped=_state["skipped"] + 1)
                continue
            if row["embedding"] and row["embedding_model"] == EMBEDDING_MODEL:
                _set_status(processed=_state["processed"] + 1, skipped=_state["skipped"] + 1)
                continue
            try:
                values = gemini.embed(f"{row['kind']}: {row['key']}\n{row['content']}")
                saved = _save_embedding(memory_id, values) if values else 0
                if saved:
                    _set_status(processed=_state["processed"] + 1, embedded=_state["embedded"] + 1)
                else:
                    _set_status(processed=_state["processed"] + 1, skipped=_state["skipped"] + 1)
            except Exception as exc:
                _set_status(processed=_state["processed"] + 1, failed=_state["failed"] + 1, last_error=str(exc)[:500])
            _stop.wait(POLL_SECONDS)
        _set_status("completed")
    except Exception as exc:
        _set_status("failed", last_error=str(exc)[:500])


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
