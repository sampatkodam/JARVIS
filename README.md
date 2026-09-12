# JARVIS V0.2

Lightweight, cloud-first autonomous personal agent with a persistent background task queue and live execution logs.

## Included
- FastAPI backend
- SQLite persistent task, step, and log state
- Persistent background task queue
- Task states: `waiting`, `running`, `retrying`, `failed`, `completed` (plus `cancelled`)
- Background worker started with the FastAPI application
- Automatic retry with bounded exponential backoff (up to 3 retries)
- Persistent per-task live logs with UTC timestamps and log levels
- Per-task log API with incremental `after_id` streaming semantics
- Automatic web UI refresh every 2 seconds
- Gemini API via official `google-genai` SDK
- Workspace-scoped filesystem tools
- Shell tool with timeout/output limits
- Git tools
- Planner -> Executor -> Critic loop
- No local LLM/Ollama required

## Requirements
- Python 3.11+
- Git
- Gemini API key with API access enabled

## Run (Windows PowerShell)
```powershell
cd jarvis-v0.1
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
uvicorn app.main:app --reload
```

Open http://127.0.0.1:8000

The worker starts automatically with FastAPI. Creating a task puts it in `waiting`; the worker claims it and changes it to `running`. Transient execution failures move the task to `retrying`, then back to `waiting` when the backoff expires. After the retry limit, the task becomes `failed`.

## Live logs
Each task has a durable `task_logs` history in SQLite. Worker lifecycle events, planning cycles, tool calls/results, step outcomes, retries, recovery, completion, and errors are recorded with timestamps and levels.

The UI displays the log console for every task and automatically polls every 2 seconds. The browser keeps a per-task log cursor and requests only entries newer than the last received log ID, avoiding repeated transfer of the full history during normal operation.

API:
- `GET /api/tasks/{task_id}/logs?after_id=0&limit=500`

`after_id` enables incremental polling; `limit` is capped at 1000.

## Queue behavior
1. API creates a task in `waiting` and writes the first log entry.
2. The worker atomically claims one eligible task and marks it `running`.
3. Worker lifecycle and execution events are persisted to `task_logs`.
4. The existing planner/executor/critic loop runs in the worker.
5. Successful execution ends in `completed`.
6. Worker exceptions enter `retrying` with a bounded backoff and a log entry.
7. After 3 retries the task becomes `failed`.
8. Task metadata, retry count, next-run time, heartbeat, log history, and step history survive process restarts because they are stored in SQLite.
9. Stale running tasks are recovered when the worker starts.

## API
- `GET /api/health` — service and worker status
- `GET /api/tasks` — queue/task list
- `POST /api/tasks` — enqueue a new task
- `GET /api/tasks/{task_id}` — task and step status
- `GET /api/tasks/{task_id}/logs` — incremental task log history
- `POST /api/tasks/{task_id}/run` — requeue a failed/cancelled task
- `POST /api/tasks/{task_id}/cancel` — cancel a queued/retrying/running task

## Autonomy
JARVIS retains the V0.1 planner -> executor -> critic loop. V0.2 moves execution out of the HTTP request path and into a persistent SQLite-backed worker queue, while V0.2.x adds durable execution telemetry that can be consumed incrementally by the dashboard.

This remains a development baseline. Its shell safety policy is intentionally conservative but is not a production-grade sandbox.
