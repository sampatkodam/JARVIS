# JARVIS V0.2

Lightweight, cloud-first autonomous personal agent with a persistent background task queue.

## Included
- FastAPI backend
- SQLite persistent task and step state
- Persistent background task queue
- Task states: `waiting`, `running`, `retrying`, `failed`, `completed` (plus `cancelled`)
- Background worker started with the FastAPI application
- Automatic retry with bounded exponential backoff (up to 3 retries)
- Gemini API via official `google-genai` SDK
- Workspace-scoped filesystem tools
- Shell tool with timeout/output limits
- Git tools
- Planner -> Executor -> Critic loop
- Live task-status updates in the web UI
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

## Run (macOS/Linux)
```bash
cd jarvis-v0.1
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
nano .env
uvicorn app.main:app --reload
```

## Configuration
Set `GEMINI_API_KEY` in `.env`.

Optional:
- `JARVIS_DB_PATH`
- `JARVIS_WORKSPACE`
- `JARVIS_COMMAND_TIMEOUT`
- `JARVIS_MAX_OUTPUT_CHARS`

## Queue behavior
1. API creates a task in `waiting`.
2. The worker atomically claims one eligible task and marks it `running`.
3. The existing planner/executor/critic loop runs in the worker.
4. Successful execution ends in `completed`.
5. Worker exceptions enter `retrying` with a bounded backoff.
6. After 3 retries the task becomes `failed`.
7. Task metadata, retry count, next-run time, heartbeat, and step history survive process restarts because they are stored in SQLite.
8. The web UI polls task state and worker health every 2 seconds.

## API
- `GET /api/health` — service and worker status
- `GET /api/tasks` — queue/task list
- `POST /api/tasks` — enqueue a new task
- `GET /api/tasks/{task_id}` — task and step status
- `POST /api/tasks/{task_id}/run` — requeue a failed/cancelled task
- `POST /api/tasks/{task_id}/cancel` — cancel a queued/retrying/running task

## Autonomy
JARVIS retains the V0.1 planner -> executor -> critic loop. V0.2 moves execution out of the HTTP request path and into a persistent SQLite-backed worker queue, so the browser/API request can return immediately while the task continues in the background.

V0.2 is still a development baseline. Its shell safety policy is intentionally conservative but is not a production-grade sandbox.
