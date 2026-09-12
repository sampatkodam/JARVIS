from datetime import datetime, timedelta, timezone
from threading import Event, Thread

from app.agent import execute_task, log_task
from app.db import connect

POLL_INTERVAL_SECONDS = 1.0
MAX_RETRIES = 3
RECOVER_AFTER_SECONDS = 300


def now():
    """Return a timezone-aware UTC timestamp in the queue's canonical format."""
    return datetime.now(timezone.utc).isoformat()


def recover_stale_tasks():
    """Return interrupted running tasks to the durable queue after a restart/crash."""
    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=RECOVER_AFTER_SECONDS)).isoformat()
    recovered = []
    with connect() as conn:
        rows = conn.execute(
            """SELECT id FROM tasks
               WHERE status='running'
                 AND (worker_heartbeat_at IS NULL OR worker_heartbeat_at < ?)""",
            (cutoff,),
        ).fetchall()
        for row in rows:
            task_id = row["id"]
            changed = conn.execute(
                """UPDATE tasks SET status='waiting', next_run_at=NULL, worker_heartbeat_at=NULL,
                   error=COALESCE(error, 'Recovered after worker interruption.'), updated_at=?
                   WHERE id=? AND status='running'""",
                (now(), task_id),
            ).rowcount
            if changed:
                recovered.append(task_id)
    for task_id in recovered:
        log_task(task_id, "Recovered stale task after worker restart.", "warning")


def claim_next_task():
    """Atomically claim the oldest eligible task so concurrent workers cannot duplicate a claim."""
    task_id = None
    with connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        current = now()
        row = conn.execute(
            """SELECT id FROM tasks
               WHERE status IN ('waiting', 'retrying')
                 AND (next_run_at IS NULL OR next_run_at <= ?)
               ORDER BY created_at ASC LIMIT 1""",
            (current,),
        ).fetchone()
        if row:
            candidate = row["id"]
            changed = conn.execute(
                """UPDATE tasks SET status='running', updated_at=?, worker_heartbeat_at=?
                   WHERE id=? AND status IN ('waiting', 'retrying')""",
                (current, current, candidate),
            ).rowcount
            if changed:
                task_id = candidate
    if task_id:
        log_task(task_id, "Task claimed by worker.")
    return task_id


def mark_retry(task_id: str, error: str):
    message = None
    level = "warning"
    with connect() as conn:
        row = conn.execute("SELECT retry_count, status FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row or row["status"] != "running":
            return
        retries = int(row["retry_count"] or 0) + 1
        if retries <= MAX_RETRIES:
            delay = min(60, 2 ** retries)
            scheduled = datetime.now(timezone.utc) + timedelta(seconds=delay)
            scheduled_at = scheduled.isoformat()
            conn.execute(
                """UPDATE tasks SET status='retrying', retry_count=?, next_run_at=?,
                   error=?, worker_heartbeat_at=NULL, updated_at=? WHERE id=? AND status='running'""",
                (retries, scheduled_at, error, now(), task_id),
            )
            message = f"Retry {retries}/{MAX_RETRIES} scheduled in {delay}s: {error}"
        else:
            conn.execute(
                """UPDATE tasks SET status='failed', retry_count=?, error=?,
                   worker_heartbeat_at=NULL, next_run_at=NULL, updated_at=?
                   WHERE id=? AND status='running'""",
                (retries, error, now(), task_id),
            )
            message = f"Retry limit reached; task failed: {error}"
            level = "error"
    if message:
        log_task(task_id, message, level)


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
