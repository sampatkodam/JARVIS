# JARVIS V0.4

Cloud-first autonomous personal agent with a persistent queue, live task logs, safe task controls, durable searchable memory, and an OS-isolated execution sandbox.

## Included
- FastAPI backend
- SQLite persistent task, step, log, conversation, message, and memory state
- Persistent background task queue with atomic multi-worker claiming
- UTC retry scheduling with bounded backoff
- Persistent pause/cancellation controls
- Live per-task log history and incremental polling
- Conversation history persisted across restarts
- Task history retained as durable agent context
- Project, workspace, task, conversation, and global memory scopes
- SQLite FTS5 searchable memory storage
- Gemini-powered memory extraction from conversations and completed/failed tasks
- Gemini-powered workspace/project memory extraction endpoint
- Explicit memory write API for durable decisions, constraints, preferences, and facts
- Gemini API via official `google-genai` SDK
- Workspace-scoped filesystem tools
- Docker-backed OS sandbox for autonomous shell/Git execution
- Digest-pinned sandbox image verification; mutable tags are rejected
- Network, filesystem, process, CPU, memory, capability, and privilege isolation
- Planner -> Executor -> Critic loop
- No local LLM/Ollama required

## Sandbox provenance and pinning
Autonomous tool execution never falls back to the host shell. JARVIS requires `JARVIS_SANDBOX_IMAGE` to be an immutable Docker reference of the form:

```text
registry.example/jarvis-sandbox@sha256:<64-hex-digest>
```

Mutable references such as `jarvis-sandbox:latest` or `jarvis-sandbox:v1` are rejected before execution. The daemon's `RepoDigests` are checked against the requested digest immediately before container creation; a missing or mismatched digest fails closed.

The checked-in `Dockerfile.sandbox` also pins its Python base image by digest. Production deployments should publish the built sandbox image to a trusted registry and configure the exact resulting manifest digest in `JARVIS_SANDBOX_IMAGE`. The test workflow creates a temporary registry, pushes the built image, obtains its immutable digest, and runs the sandbox isolation suite against that digest.

Sandbox containers mount only the configured workspace read/write. The container root filesystem is read-only, `/tmp` is a bounded `noexec` tmpfs, Linux capabilities are dropped, `no-new-privileges` is enabled, and CPU/memory/process limits are enforced. Network access is disabled by default; dependency installation commands receive explicit temporary bridge networking.

## Persistent memory architecture
Memory is stored in the same durable SQLite database as the queue. `conversations` and `messages` preserve the chat timeline. `tasks` and `steps` preserve execution history. `memories` stores normalized durable facts with scope, kind, key, source, confidence, and timestamps. SQLite FTS5 provides indexed full-text search without adding a vector-database dependency.

Supported scopes:
- `global` — durable facts/preferences that apply broadly
- `project` — project-specific decisions, constraints, architecture, and lessons
- `workspace` — facts extracted from the configured JARVIS workspace
- `task` — facts learned from an individual execution
- `conversation` — facts tied to a conversation

Memory extraction is conservative: Gemini is instructed to keep only durable facts supported by the supplied material and to reject secrets such as API keys, passwords, tokens, and private keys.

## Conversation flow
1. Create a conversation with `POST /api/conversations`.
2. Send a message with `POST /api/conversations/{id}/messages`.
3. JARVIS retrieves matching long-term memory, recent task history, and conversation history before asking Gemini for the answer.
4. The user and assistant messages are persisted.
5. Gemini extracts durable facts from the exchange and upserts them into searchable memory.

## Task memory flow
When a worker reaches `completed` or `failed`, V0.4 sends the task goal/result and execution steps through the Gemini memory extractor. Durable project/task lessons are persisted without storing secrets. Queue execution remains independent if Gemini memory extraction fails.

## Workspace/project memory
`POST /api/memory/workspace/extract` scans a bounded set of small text/config/source files under the configured workspace and asks Gemini to extract durable workspace/project facts. It is deliberately bounded to avoid loading an entire repository into a model request.

Project-specific facts can also be written directly through `POST /api/memory` with `scope=project` and a `scope_id` such as a project name.

## Search
`GET /api/memory/search?q=...&limit=20` performs SQLite FTS5 search over stored memories and returns scope, kind, key, content, source, confidence, and timestamps.

## API
- `GET /api/health` — service and worker status
- `GET /api/tasks` — task queue/history
- `POST /api/tasks` — enqueue a task
- `GET /api/tasks/{task_id}` — task and step history
- `GET /api/tasks/{task_id}/logs` — incremental task logs
- `POST /api/tasks/{task_id}/run` — requeue a failed/cancelled task
- `POST /api/tasks/{task_id}/pause` — pause
- `POST /api/tasks/{task_id}/resume` — resume
- `POST /api/tasks/{task_id}/cancel` — cancel
- `POST /api/conversations` — create persistent conversation
- `GET /api/conversations/{conversation_id}` — retrieve conversation history
- `POST /api/conversations/{conversation_id}/messages` — chat using memory + task history
- `GET /api/memory/search?q=...` — search durable memory
- `GET /api/memory/tasks` — task-history memory view
- `POST /api/memory` — write an explicit durable memory
- `POST /api/memory/workspace/extract` — Gemini-extract workspace memory

## Run (Windows PowerShell)
```powershell
py -3.11 -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
uvicorn app.main:app --reload
```

Before autonomous execution, configure `JARVIS_SANDBOX_IMAGE` with the exact digest of the trusted sandbox image. If it is absent, malformed, mutable, unavailable, or mismatched, JARVIS refuses execution rather than falling back to the host.

Open http://127.0.0.1:8000

## Tests
```powershell
python -m unittest discover -s tests -v
```

The tests cover queue concurrency/retry behavior, persistent memory, conversation history, Gemini extraction, sandbox filesystem/network/resource isolation, mutable-tag rejection, digest mismatch rejection, and successful digest verification.

## Data and privacy
The memory database is local SQLite by default. Only material explicitly sent to Gemini for extraction/chat is processed by the configured Gemini API. Memory extraction filters obvious secret material and does not intentionally persist credentials. The workspace extractor is bounded to small source/config/text files.

The autonomous execution path is now fail-closed behind an OS-level Docker sandbox. The remaining trust boundary includes the Docker daemon and the configured trusted image registry; sandbox image provenance is therefore verified by immutable digest before every execution.
