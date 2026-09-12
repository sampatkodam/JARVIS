import time
from datetime import datetime, timezone
from threading import Event, Thread

from app.agent import execute_task
from app.db import connect

POLL_INTERVAL_SECONDS = 1.0
MAX_RETRIES = 3


def now():
    return datetime.now(timezone.utc).isoformat()


def claim_next_task():
    """Atomically claim one waiting/retrying task so multiple workers are safe."""
    with connect() as conn:
        row = conn.execute(
            """SELECT id FROM tasks
               WHERE status IN ('waiting', 'retrying')
                 AND (next_run_at IS NULL OR next_run_at <= ?)
               ORDER BY created_at ASC LIMIT 1""",
            (now(),),
        ).fetchone()
        if not row:
            return None
        task_id = row["id"]
        updated = conn.execute(
            """UPDATE tasks
               SET status='running', updated_at=?, worker_heartbeat_at=?
               WHERE id=? AND status IN ('waiting', 'retrying')""",
            (now(), now(), task_id),
        ).rowcount
        return task_id if updated else None


def mark_retry(task_id: str, error: str):
    with connect() as conn:
        row = conn.execute(
            "SELECT retry_count FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        retries = int(row["retry_count"] if row else 0) + 1
        if retries <= MAX_RETRIES:
            # Small fixed backoff keeps V0.2 deterministic while preventing a hot loop.
            delay = min(60, 2 ** retries)
            conn.execute(
                """UPDATE tasks
                   SET status='retrying', retry_count=?, next_run_at=datetime('now', ?),
                       error=?, updated_at=?
                   WHERE id=?""",
                (retries, f"+{delay} seconds", error, now(), task_id),
            )
        else:
            conn.execute(
                """UPDATE tasks SET status='failed', retry_count=?, error=?, updated_at=?
                   WHERE id=?""",
                (retries, error, now(), task_id),
            )


def worker_loop(stop: Event):
    while not stop.is_set():
        task_id = claim_next_task()
        if not task_id:
            stop.wait(POLL_INTERVAL_SECONDS)
            continue
        try:
            execute_task(task_id)
        except Exception as exc:
            mark_retry(task_id, str(exc))


class TaskWorker:
    def __init__(self):
        self.stop_event = Event()
        self.thread = None

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        self.thread = Thread(target=worker_loop, args=(self.stop_event,), daemon=True, name="jarvis-task-worker")
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=3)
