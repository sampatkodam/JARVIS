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
- Required Cosign signature verification with rotating trusted signing keys and explicit key IDs
- Required Sigstore/Rekor transparency-log inclusion-proof verification
- Explicit trusted Rekor log identity and signed checkpoint-state validation
- Rotating trusted Rekor checkpoint signing keys with explicit key IDs
- Revoked signing and Rekor keys are never accepted
- Network, filesystem, process, CPU, memory, capability, and privilege isolation
- Planner -> Executor -> Critic loop
- No local LLM/Ollama required

## Sandbox provenance, signing, transparency, pinning, and key rotation
Autonomous tool execution never falls back to the host shell. JARVIS requires `JARVIS_SANDBOX_IMAGE` to be an immutable Docker reference of the form:

```text
registry.example/jarvis-sandbox@sha256:<64-hex-digest>
```

Mutable references such as `jarvis-sandbox:latest` or `jarvis-sandbox:v1` are rejected before execution. The daemon's `RepoDigests` are checked against the requested digest immediately before container creation; a missing or mismatched digest fails closed.

JARVIS requires a valid Cosign signature from an active trusted signing key in `config/sandbox-signing-policy.json`. The policy supports multiple trusted signing keys, stable explicit key IDs, an optional `key_id` to select one signing key during a controlled rotation, and per-key `revoked` state. A revoked signing key is excluded before verification and therefore cannot authorize an image.

The transparency policy independently supports multiple trusted Rekor checkpoint keys. Each Rekor key has its own stable ID, public-key environment variable, Rekor URL, expected log ID, checkpoint-origin prefix, and `revoked` state. JARVIS can therefore trust an old and replacement Rekor checkpoint signing key simultaneously during a rotation, or select one explicitly with `transparency_log.key_id`.

For every active candidate pair, JARVIS invokes Cosign against the exact image digest and the candidate Rekor endpoint. It supplies the candidate Rekor public key through `SIGSTORE_REKOR_PUBLIC_KEY`, so Cosign verifies the checkpoint with the exact configured key rather than silently relying on unrelated or stale trust material. JARVIS then requires the verification result to contain an inclusion proof whose log identity exactly matches that Rekor key's pinned `log_id`.

The proof must contain a valid SHA-256 root hash, proof hashes, valid log/tree indexes, and a signed checkpoint envelope. The checkpoint is explicitly parsed and checked: its origin must match the selected Rekor key's origin prefix, its tree size and root hash must exactly match the inclusion proof, and its signature line must identify the expected Rekor host. Cosign remains responsible for cryptographically verifying the image signature, Merkle inclusion, and checkpoint signature; JARVIS adds explicit key identity/state checks so a proof from an unexpected log, a checkpoint inconsistent with the proof, or a revoked checkpoint key cannot authorize execution.

The checked-in policy contains both the established `rekor.sigstore.dev` trust entry and the current `log2025-1.rekor.sigstore.dev` trust entry from Sigstore's public trusted root. The old entry can be revoked after an operational cutover without removing the replacement entry. During a controlled rotation, keep both public-key files configured, validate the replacement, then mark the retired Rekor key `revoked: true` and optionally set `transparency_log.key_id` to the replacement for deterministic selection.

Missing keys, unsigned images, invalid signatures, revoked-key attempts, malformed policies, unknown key IDs, verifier errors, unexpected Rekor log IDs, invalid checkpoints, and transparency verification failures all fail closed. The repository contains no signing private keys.

The checked-in `Dockerfile.sandbox` also pins its Python base image by digest. The CI workflow builds and pushes an ephemeral test image, obtains its immutable digest, and exercises the sandbox against that digest when the required signing/transparency trust material is available. Regression tests cover digest pinning, signature-key rotation, Rekor checkpoint-key rotation, explicit key selection, revoked-key rejection, missing inclusion proofs, unexpected Rekor log IDs, invalid checkpoints, checkpoint state mismatches, valid checkpoint acceptance, and malformed verifier output.

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

Before autonomous execution, configure `JARVIS_SANDBOX_IMAGE` with the exact trusted image digest, configure the trusted Cosign public keys, and configure every active Rekor public key referenced by the sandbox signing policy. During a Rekor checkpoint-key rotation, keep the old and replacement keys configured until the replacement is verified; then revoke the retired key in policy. If `transparency_log.key_id` is set, only that Rekor key is eligible and it must not be revoked. If no active trusted signing/Rekor key pair can verify the exact digest and its expected Rekor log/checkpoint state, JARVIS refuses execution rather than falling back to the host.

Open http://127.0.0.1:8000

## Tests
```powershell
python -m unittest discover -s tests -v
```

The tests cover queue concurrency/retry behavior, persistent memory, conversation history, Gemini extraction, sandbox filesystem/network/resource isolation, mutable-tag rejection, digest mismatch rejection, signing-key rotation, Rekor checkpoint-key rotation, explicit key selection, revoked signing/Rekor key rejection, missing transparency evidence, unexpected Rekor log IDs, invalid checkpoints, checkpoint/root/tree-state mismatch, valid checkpoint acceptance, and malformed verifier output.

## Data and privacy
The memory database is local SQLite by default. Only material explicitly sent to Gemini for extraction/chat is processed by the configured Gemini API. Memory extraction filters obvious secret material and does not intentionally persist credentials. The workspace extractor is bounded to small source/config/text files.

The autonomous execution path is fail-closed behind an OS-level Docker sandbox with immutable, signed image provenance, mandatory Sigstore transparency evidence, explicit Rekor log identity validation, checkpoint-state validation, and independent checkpoint-key rotation/revocation policy. The remaining trust boundary includes the Docker daemon, the configured trusted image registry, Sigstore's trusted root used by Cosign, and the operator-provisioned Cosign/Rekor trust keys and policy.
