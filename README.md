# JARVIS V0.1

Lightweight, cloud-first autonomous personal agent baseline.

Included:
- FastAPI backend
- SQLite persistent task state
- Gemini API via official `google-genai` SDK
- Workspace-scoped filesystem tools
- Shell tool with timeout/output limits
- Git tools
- Planner -> Executor -> Critic loop
- Persistent task/step state
- Basic web UI
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

## API
- `GET /api/health`
- `GET /api/tasks`
- `POST /api/tasks`
- `GET /api/tasks/{task_id}`
- `POST /api/tasks/{task_id}/run`
- `POST /api/tasks/{task_id}/cancel`

## Autonomy
Each task uses:
1. Planner creates a small executable plan.
2. Executor performs a step.
3. Critic evaluates it.
4. Failed steps trigger replanning/recovery.
5. State is persisted after transitions.
6. The loop stops only on verified completion, cancellation, or a hard execution limit.

V0.1 is a development baseline. Its shell safety policy is intentionally conservative but is not a production-grade sandbox.
