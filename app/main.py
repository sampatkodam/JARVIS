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
from app.gemini import Gemini
from app.memory import add_message, conversation_history, extract_memories, extract_workspace_memory, memory_context, new_conversation, search_memories, task_history

worker = TaskWorker()

@asynccontextmanager
async def lifespan(app):
    worker.start()
    yield
    worker.stop()

app = FastAPI(title="JARVIS V0.4", version="0.4.0", lifespan=lifespan)
STATIC = BASE_DIR / "web"
app.mount("/static", StaticFiles(directory=STATIC), name="static")

@app.get("/")
def index():
    return FileResponse(STATIC / "index.html")

@app.get("/api/health")
def health():
    alive = bool(worker.thread and worker.thread.is_alive())
    return {"status":"ok", "service":"jarvis", "version":"0.4.0", "worker":"running" if alive else "stopped"}

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
    if not result: raise HTTPException(404, "Task not found")
    return result

@app.get("/api/tasks/{task_id}/logs")
def task_logs(task_id: str, after_id: int = Query(0, ge=0), limit: int = Query(500, ge=1, le=1000)):
    if not get_task(task_id): raise HTTPException(404, "Task not found")
    return {"task_id":task_id, "logs":get_task_logs(task_id, after_id, limit)}

@app.post("/api/tasks/{task_id}/run", response_model=RunResponse)
def run(task_id: str):
    if not get_task(task_id): raise HTTPException(404, "Task not found")
    with connect() as conn:
        changed = conn.execute("""UPDATE tasks SET status='waiting', next_run_at=NULL, error=NULL,
            pause_requested=0, cancel_requested=0, updated_at=? WHERE id=? AND status IN ('failed','cancelled')""", (datetime.now(timezone.utc).isoformat(), task_id)).rowcount
    if changed: log_task(task_id, "Task manually requeued.")
    return RunResponse(task_id=task_id, status="waiting", message="Task queued for background execution.")

@app.post("/api/tasks/{task_id}/pause")
def pause(task_id: str):
    if not get_task(task_id): raise HTTPException(404, "Task not found")
    status = request_pause(task_id)
    if status is None: raise HTTPException(404, "Task not found")
    return {"task_id":task_id, "status":status, "message":"Pause accepted." if status in ("paused","pausing") else "Task is not running."}

@app.post("/api/tasks/{task_id}/resume")
def resume(task_id: str):
    if not get_task(task_id): raise HTTPException(404, "Task not found")
    status = resume_task(task_id)
    if status is None: raise HTTPException(404, "Task not found")
    return {"task_id":task_id, "status":status, "message":"Task queued for background execution." if status == "waiting" else "Task was not paused."}

@app.post("/api/tasks/{task_id}/cancel")
def cancel(task_id: str):
    if not get_task(task_id): raise HTTPException(404, "Task not found")
    status = request_cancel(task_id)
    if status is None: raise HTTPException(404, "Task not found")
    return {"task_id":task_id, "status":status, "message":"Cancellation accepted." if status in ("cancelled","cancelling") else "Task is already terminal."}

@app.post("/api/conversations")
def create_conversation(title: str | None = None):
    return {"conversation_id": new_conversation(title)}

@app.get("/api/conversations/{conversation_id}")
def get_conversation(conversation_id: str, limit: int = Query(40, ge=1, le=200)):
    return {"conversation_id":conversation_id, "messages":conversation_history(conversation_id, limit)}

@app.post("/api/conversations/{conversation_id}/messages")
def conversation_message(conversation_id: str, payload: dict):
    content = str(payload.get("content", "")).strip()
    if not content or len(content) > 20000: raise HTTPException(400, "content is required and must be <= 20000 characters")
    add_message(conversation_id, "user", content)
    history = conversation_history(conversation_id, 40)
    prompt = "Answer the user using the conversation history and relevant long-term memory. Be accurate and do not claim actions you did not perform.\n\nLONG-TERM MEMORY:\n" + memory_context(content) + "\n\nTASK HISTORY:\n" + "\n".join(str(x) for x in task_history(8)) + "\n\nCONVERSATION:\n" + "\n".join(f"{m['role']}: {m['content']}" for m in history)
    answer = Gemini().json(prompt, "You are JARVIS. Return JSON: {\"answer\":\"...\"}").get("answer", "")
    add_message(conversation_id, "assistant", answer)
    extract_memories(f"User: {content}\nAssistant: {answer}", scope="global", source_type="conversation", source_id=conversation_id)
    return {"conversation_id":conversation_id, "answer":answer, "messages":conversation_history(conversation_id, 40)}

@app.get("/api/memory/search")
def memory_search(q: str = Query(..., min_length=1), limit: int = Query(20, ge=1, le=100)):
    return {"query":q, "memories":search_memories(q, limit)}

@app.get("/api/memory/tasks")
def memory_tasks(limit: int = Query(30, ge=1, le=100)):
    return {"tasks":task_history(limit)}

@app.post("/api/memory/workspace/extract")
def memory_workspace_extract():
    ids = extract_workspace_memory()
    return {"stored_memory_ids":ids, "count":len(ids)}
