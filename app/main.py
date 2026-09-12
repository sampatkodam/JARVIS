from contextlib import asynccontextmanager
from datetime import datetime, timezone
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from app.models import CreateTask, RunResponse
from app.agent import create_task, get_task, get_task_logs, request_pause, request_cancel, resume_task, log_task
from app.db import connect
from app.config import BASE_DIR
from app.queue import TaskWorker

worker = TaskWorker()


@asynccontextmanager
async def lifespan(app):
    worker.start()
    yield
    worker.stop()


app = FastAPI(title="JARVIS V0.3", version="0.3.0", lifespan=lifespan)
STATIC = BASE_DIR / "web"
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
def health():
    alive = bool(worker.thread and worker.thread.is_alive())
    return {"status": "ok", "service": "jarvis", "version": "0.3.0", "worker": "running" if alive else "stopped"}


@app.get("/api/tasks")
def tasks():
    with connect() as conn:
        return [dict(r) for r in conn.execute("SELECT * FROM tasks ORDER BY created_at DESC").fetchall()]


@app.post("/api/tasks", status_code=201)
def new_task(payload: CreateTask):
    task_id = create_task(payload.goal)
    return get_task(task_id)


@app.get("/api/tasks/{task_id}")
def task(task_id: str):
    result = get_task(task_id)
    if not result:
        raise HTTPException(404, "Task not found")
    return result


@app.get("/api/tasks/{task_id}/logs")
def task_logs(task_id: str, after_id: int = Query(0, ge=0), limit: int = Query(500, ge=1, le=1000)):
    if not get_task(task_id):
        raise HTTPException(404, "Task not found")
    return {"task_id": task_id, "logs": get_task_logs(task_id, after_id, limit)}


@app.post("/api/tasks/{task_id}/run", response_model=RunResponse)
def run(task_id: str):
    record = get_task(task_id)
    if not record:
        raise HTTPException(404, "Task not found")
    with connect() as conn:
        changed = conn.execute(
            """UPDATE tasks SET status='waiting', next_run_at=NULL, error=NULL,
               pause_requested=0, cancel_requested=0, updated_at=?
               WHERE id=? AND status IN ('failed','cancelled')""",
            (datetime.now(timezone.utc).isoformat(), task_id),
        ).rowcount
    if changed:
        log_task(task_id, "Task manually requeued.")
    return RunResponse(task_id=task_id, status="waiting", message="Task queued for background execution.")


@app.post("/api/tasks/{task_id}/pause")
def pause(task_id: str):
    if not get_task(task_id):
        raise HTTPException(404, "Task not found")
    status = request_pause(task_id)
    if status is None:
        raise HTTPException(404, "Task not found")
    return {"task_id": task_id, "status": status, "message": "Pause accepted." if status in ("paused", "pausing") else "Task is not running."}


@app.post("/api/tasks/{task_id}/resume")
def resume(task_id: str):
    if not get_task(task_id):
        raise HTTPException(404, "Task not found")
    status = resume_task(task_id)
    if status is None:
        raise HTTPException(404, "Task not found")
    return {"task_id": task_id, "status": status, "message": "Task queued for background execution." if status == "waiting" else "Task was not paused."}


@app.post("/api/tasks/{task_id}/cancel")
def cancel(task_id: str):
    if not get_task(task_id):
        raise HTTPException(404, "Task not found")
    status = request_cancel(task_id)
    if status is None:
        raise HTTPException(404, "Task not found")
    return {"task_id": task_id, "status": status, "message": "Cancellation accepted." if status in ("cancelled", "cancelling") else "Task is already terminal."}
