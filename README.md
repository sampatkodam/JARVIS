# JARVIS V0.3.1

Lightweight, cloud-first autonomous personal agent with a persistent background task queue, live execution logs, safe task controls, and hardened queue scheduling/claiming.

## Included
- FastAPI backend
- SQLite persistent task, step, and log state
- Persistent background task queue
- Task states: `waiting`, `running`, `retrying`, `paused`, `failed`, `completed`, `cancelled`
- Persistent pause/cancellation request flags for running tasks
- Background worker started with the FastAPI application
- Clean worker checkpoints for pause and cancellation
- Automatic retry with bounded exponential backoff (up to 3 retries)
- Canonical timezone-aware UTC timestamps for queue metadata
- Atomic multi-worker task claiming using SQLite `BEGIN IMMEDIATE`
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

The worker starts automatically with FastAPI. Creating a task puts it in `waiting`; the worker claims it and changes it to `running`. Transient execution failures move the task to `retrying`, then become eligible again only after the persisted UTC backoff timestamp is reached. Tasks can be paused, resumed, or cancelled from the dashboard.

## Queue hardening
- All queue-generated timestamps use timezone-aware UTC ISO-8601 strings, including `next_run_at`.
- Retry scheduling is calculated in Python and persisted as an absolute UTC timestamp instead of SQLite's timezone-less `datetime()` text.
- Eligibility compares canonical ISO timestamps consistently, so retry tasks cannot run early because of timestamp-format mismatches.
- Task claiming uses a SQLite `BEGIN IMMEDIATE` transaction around selection and state transition. Concurrent workers therefore serialize the claim operation and only one worker can transition a particular task from `waiting`/`retrying` to `running`.
- Claim/retry lifecycle logs are written after the queue transaction is released, avoiding nested SQLite write locks.

## Task controls
- **Pause** on `waiting`/`retrying` tasks moves them immediately to `paused` and removes them from the runnable queue.
- **Pause** on a `running` task persists `pause_requested=1`; the worker finishes the current tool/critic boundary and then transitions to `paused` at a safe checkpoint.
- **Resume** moves a `paused` task back to `waiting`, clears control flags, and allows the worker to continue.
- **Cancel** on a queued or paused task moves it immediately to `cancelled`.
- **Cancel** on a `running` task persists `cancel_requested=1`; the worker finishes the current tool boundary, stops before another step, clears its heartbeat, and transitions to `cancelled`.
- `pausing` and `cancelling` are UI-facing transitional states represented by the persisted control request plus the current running state; the worker owns the final safe transition.
- Completed and failed tasks remain terminal. A failed/cancelled task can be explicitly queued again with **Queue again**.

## Live logs
Each task has a durable `task_logs` history in SQLite. Worker lifecycle events, planning cycles, tool calls/results, step outcomes, retries, recovery, pause/resume/cancel requests, completion, and errors are recorded with timestamps and levels.

The UI displays the log console for every task and automatically polls every 2 seconds. The browser keeps a per-task log cursor and requests only entries newer than the last received log ID, avoiding repeated transfer of the full history during normal operation.

API:
- `GET /api/tasks/{task_id}/logs?after_id=0&limit=500`

`after_id` enables incremental polling; `limit` is capped at 1000.

## Tests
Queue hardening tests use Python's standard-library `unittest`, so no additional test dependency is required.

```powershell
python -m unittest discover -s tests -v
```

The test suite verifies:
- two concurrent workers cannot both claim the same task;
- retry timestamps are timezone-aware UTC ISO-8601 values;
- retry backoff is persisted as an absolute future UTC timestamp;
- a retrying task is not claimable before its backoff expires;
- the task becomes claimable after the scheduled backoff.

## Queue behavior
1. API creates a task in `waiting` and writes the first log entry.
2. The worker atomically claims one eligible task and marks it `running`.
3. Worker lifecycle and execution events are persisted to `task_logs`.
4. The planner/executor/critic loop runs in the worker.
5. Pause/cancel requests are persisted and checked at safe execution checkpoints.
6. Successful execution ends in `completed`.
7. Worker exceptions enter `retrying` with bounded UTC backoff and a log entry.
8. After 3 retries the task becomes `failed`.
9. Task metadata, control flags, retry count, next-run time, heartbeat, log history, and step history survive process restarts because they are stored in SQLite.
10. Stale running tasks are recovered when the worker starts.

## API
- `GET /api/health` — service and worker status
- `GET /api/tasks` — queue/task list
- `POST /api/tasks` — enqueue a new task
- `GET /api/tasks/{task_id}` — task and step status
- `GET /api/tasks/{task_id}/logs` — incremental task log history
- `POST /api/tasks/{task_id}/run` — requeue a failed/cancelled task
- `POST /api/tasks/{task_id}/pause` — pause a queued or running task
- `POST /api/tasks/{task_id}/resume` — resume a paused task
- `POST /api/tasks/{task_id}/cancel` — cancel a queued, paused, or running task

## Autonomy
JARVIS retains the planner -> executor -> critic loop. V0.3 adds durable task controls. V0.3.1 hardens the queue's scheduling and concurrency guarantees so future multi-worker execution has a reliable persistence layer.

This remains a development baseline. Its shell safety policy is intentionally conservative but is not a production-grade sandbox.
