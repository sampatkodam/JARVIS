from datetime import datetime, timezone
from threading import Event, Thread

from app.agent import execute_task, log_task
from app.db import connect

POLL_INTERVAL_SECONDS = 1.0
MAX_RETRIES = 3
RECOVER_AFTER_SECONDS = 300


def now():
    return datetime.now(timezone.utc).isoformat()


def recover_stale_tasks():
    """Return interrupted running tasks to the durable queue after a restart/crash."""
    with connect() as conn:
        rows = conn.execute(
            "SELECT id FROM tasks WHERE status='running' AND (worker_heartbeat_at IS NULL OR worker_heartbeat_at < datetime('now', ?))",
            (f"-{RECOVER_AFTER_SECONDS} seconds",),
        ).fetchall()
        for row in rows:
            task_id = row["id"]
            conn.execute(
                """UPDATE tasks SET status='waiting', next_run_at=NULL, worker_heartbeat_at=NULL,
                   error=COALESCE(error, 'Recovered after worker interruption.'), updated_at=?
                   WHERE id=?""",
                (now(), task_id),
            )
            log_task(task_id, "Recovered stale task after worker restart.", "warning")


def claim_next_task():
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
            """UPDATE tasks SET status='running', updated_at=?, worker_heartbeat_at=?
               WHERE id=? AND status IN ('waiting', 'retrying')""",
            (now(), now(), task_id),
        ).rowcount
        if updated:
            log_task(task_id, "Task claimed by worker.")
        return task_id if updated else None


def mark_retry(task_id: str, error: str):
    with connect() as conn:
        row = conn.execute("SELECT retry_count FROM tasks WHERE id=?", (task_id,)).fetchone()
        retries = int(row["retry_count"] if row else 0) + 1
        if retries <= MAX_RETRIES:
            delay = min(60, 2 ** retries)
            conn.execute(
                """UPDATE tasks SET status='retrying', retry_count=?, next_run_at=datetime('now', ?),
                   error=?, worker_heartbeat_at=NULL, updated_at=? WHERE id=?""",
                (retries, f"+{delay} seconds", error, now(), task_id),
            )
            log_task(task_id, f"Retry {retries}/{MAX_RETRIES} scheduled in {delay}s: {error}", "warning")
        else:
            conn.execute(
                """UPDATE tasks SET status='failed', retry_count=?, error=?, worker_heartbeat_at=NULL, updated_at=?
                   WHERE id=?""",
                (retries, error, now(), task_id),
            )
            log_task(task_id, f"Retry limit reached; task failed: {error}", "error")


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
        recover_stale_tasks()
        self.stop_event.clear()
        self.thread = Thread(target=worker_loop, args=(self.stop_event,), daemon=True, name="jarvis-task-worker")
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=3)
