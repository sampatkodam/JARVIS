from contextlib import asynccontextmanager
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from app.models import CreateTask, RunResponse
from app.agent import create_task, get_task, cancel_task
from app.db import connect
from app.config import BASE_DIR
from app.queue import TaskWorker

worker = TaskWorker()


@asynccontextmanager
async def lifespan(app):
    worker.start()
    yield
    worker.stop()


app = FastAPI(title="JARVIS V0.2", version="0.2.0", lifespan=lifespan)
STATIC = BASE_DIR / "web"
app.mount("/static", StaticFiles(directory=STATIC), name="static")


@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")


@app.get("/api/health")
def health():
    alive = bool(worker.thread and worker.thread.is_alive())
    return {"status": "ok", "service": "jarvis", "version": "0.2.0", "worker": "running" if alive else "stopped"}


@app.get("/api/tasks")
def tasks():
    with connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT * FROM tasks ORDER BY created_at DESC").fetchall()]


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


@app.post("/api/tasks/{task_id}/run", response_model=RunResponse)
def run(task_id: str):
    record = get_task(task_id)
    if not record:
        raise HTTPException(404, "Task not found")
    with connect() as conn:
        conn.execute(
            """UPDATE tasks SET status='waiting', next_run_at=NULL, error=NULL, updated_at=?
               WHERE id=? AND status IN ('failed','cancelled')""",
            (__import__('datetime').datetime.now(__import__('datetime').timezone.utc).isoformat(), task_id),
        )
    return RunResponse(task_id=task_id, status="waiting", message="Task queued for background execution.")


@app.post("/api/tasks/{task_id}/cancel")
def cancel(task_id: str):
    if not get_task(task_id):
        raise HTTPException(404, "Task not found")
    cancel_task(task_id)
    return {"task_id": task_id, "status": "cancelled"}
