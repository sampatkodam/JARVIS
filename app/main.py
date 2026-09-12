from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from app.models import CreateTask, RunResponse
from app.agent import create_task, get_task, run_task, cancel_task
from app.db import connect
from app.config import BASE_DIR

app = FastAPI(title="JARVIS V0.1", version="0.1.0")
STATIC = BASE_DIR / "web"
app.mount("/static", StaticFiles(directory=STATIC), name="static")

@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")

@app.get("/api/health")
def health():
    return {"status": "ok", "service": "jarvis", "version": "0.1.0"}

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
    if not get_task(task_id):
        raise HTTPException(404, "Task not found")
    try:
        run_task(task_id)
    except Exception as exc:
        return RunResponse(task_id=task_id, status="failed", message=str(exc))
    return RunResponse(task_id=task_id, status=get_task(task_id)["task"]["status"],
                       message="Execution finished.")

@app.post("/api/tasks/{task_id}/cancel")
def cancel(task_id: str):
    if not get_task(task_id):
        raise HTTPException(404, "Task not found")
    cancel_task(task_id)
    return {"task_id": task_id, "status": "cancelled"}
