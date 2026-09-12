import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path

from app.config import WORKSPACE
from app.db import connect
from app.gemini import Gemini

MEMORY_SYSTEM = """You are JARVIS memory extraction. Extract only durable, useful facts supported by the supplied material.
Do not invent facts. Do not store secrets, API keys, passwords, tokens, or transient chatter.
Return JSON only: {\"memories\":[{\"kind\":\"preference|fact|decision|constraint|project|workspace|lesson\",\"key\":\"short stable key\",\"content\":\"concise fact\",\"confidence\":0.0}]}
Prefer high-value facts that help future tasks. If nothing durable is present, return {\"memories\":[]}."""


def now():
    return datetime.now(timezone.utc).isoformat()


def workspace_id():
    return str(WORKSPACE)


def upsert_memory(scope, scope_id, kind, key, content, source_type="manual", source_id=None, confidence=1.0):
    content = str(content).strip()[:12000]
    key = str(key).strip()[:200]
    if not content or not key:
        return None
    with connect() as conn:
        conn.execute(
            """INSERT INTO memories(scope,scope_id,kind,key,content,source_type,source_id,confidence,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)
               ON CONFLICT(scope,scope_id,kind,key) DO UPDATE SET
                 content=excluded.content, source_type=excluded.source_type,
                 source_id=excluded.source_id, confidence=excluded.confidence, updated_at=excluded.updated_at""",
            (scope, scope_id, kind, key, content, source_type, source_id, max(0.0, min(1.0, float(confidence))), now(), now()),
        )
        row = conn.execute("SELECT id FROM memories WHERE scope=? AND scope_id IS ? AND kind=? AND key=?", (scope, scope_id, kind, key)).fetchone()
        return row["id"] if row else None


def add_message(conversation_id, role, content, task_id=None):
    ts = now()
    with connect() as conn:
        conn.execute("INSERT OR IGNORE INTO conversations(id,title,created_at,updated_at) VALUES(?,?,?,?)", (conversation_id, None, ts, ts))
        conn.execute("INSERT INTO messages(conversation_id,role,content,task_id,created_at) VALUES(?,?,?,?,?)", (conversation_id, role, str(content)[:50000], task_id, ts))
        conn.execute("UPDATE conversations SET updated_at=? WHERE id=?", (ts, conversation_id))


def conversation_history(conversation_id, limit=40):
    limit = max(1, min(int(limit), 200))
    with connect() as conn:
        rows = conn.execute("SELECT id,conversation_id,role,content,task_id,created_at FROM messages WHERE conversation_id=? ORDER BY id DESC LIMIT ?", (conversation_id, limit)).fetchall()
    return [dict(r) for r in reversed(rows)]


def task_history(limit=30):
    limit = max(1, min(int(limit), 100))
    with connect() as conn:
        rows = conn.execute("SELECT id,goal,status,result,error,retry_count,created_at,updated_at FROM tasks ORDER BY created_at DESC LIMIT ?", (limit,)).fetchall()
    return [dict(r) for r in rows]


def recent_task_context(limit=12):
    rows = task_history(limit)
    return "\n".join(json.dumps(r, ensure_ascii=False) for r in rows)


def search_memories(query, limit=20, scopes=None):
    query = str(query).strip()
    limit = max(1, min(int(limit), 100))
    if not query:
        return []
    tokens = re.findall(r"[A-Za-z0-9_]+", query)
    match = " OR ".join(f'"{t}"*' for t in tokens[:12])
    if not match:
        return []
    params = [match]
    scope_clause = ""
    if scopes:
        scope_clause = " AND m.scope IN (" + ",".join("?" for _ in scopes) + ")"
        params.extend(scopes)
    params.append(limit)
    with connect() as conn:
        rows = conn.execute(
            f"""SELECT m.id,m.scope,m.scope_id,m.kind,m.key,m.content,m.source_type,m.source_id,m.confidence,m.created_at,m.updated_at
                FROM memory_fts f JOIN memories m ON m.id=f.rowid
                WHERE memory_fts MATCH ?{scope_clause}
                ORDER BY bm25(memory_fts), m.updated_at DESC LIMIT ?""", params).fetchall()
    return [dict(r) for r in rows]


def memory_context(query, limit=12):
    rows = search_memories(query, limit)
    if not rows:
        return "No matching long-term memories."
    return "\n".join(f"[{r['scope']}] {r['kind']}/{r['key']}: {r['content']}" for r in rows)


def extract_memories(text, scope="global", scope_id=None, source_type="conversation", source_id=None):
    text = str(text).strip()
    if not text:
        return []
    try:
        result = Gemini().json(text[:30000], MEMORY_SYSTEM)
    except Exception:
        return []
    saved = []
    for item in result.get("memories", []) if isinstance(result, dict) else []:
        if not isinstance(item, dict):
            continue
        kind = item.get("kind") or "fact"
        key = item.get("key")
        content = item.get("content")
        if key and content and not re.search(r"(?i)(api[_ -]?key|password|secret|token|private[_ -]?key)", f"{key} {content}"):
            mid = upsert_memory(scope, scope_id, kind, key, content, source_type, source_id, item.get("confidence", 0.7))
            if mid:
                saved.append(mid)
    return saved


def extract_workspace_memory():
    """Ask Gemini to summarize durable workspace/project facts from small text/config files."""
    chunks = []
    for path in sorted(WORKSPACE.rglob("*")):
        if not path.is_file() or path.stat().st_size > 30000:
            continue
        if path.suffix.lower() not in {".md", ".txt", ".json", ".yaml", ".yml", ".toml", ".py", ".js", ".ts"}:
            continue
        try:
            chunks.append(f"FILE: {path.relative_to(WORKSPACE)}\n{path.read_text(encoding='utf-8')[:12000]}")
        except (OSError, UnicodeDecodeError):
            continue
        if len(chunks) >= 12:
            break
    if not chunks:
        return []
    return extract_memories("Workspace evidence:\n" + "\n\n".join(chunks), scope="workspace", scope_id=workspace_id(), source_type="workspace", source_id=workspace_id())


def new_conversation(title=None):
    conversation_id = str(uuid.uuid4())
    ts = now()
    with connect() as conn:
        conn.execute("INSERT INTO conversations(id,title,created_at,updated_at) VALUES(?,?,?,?)", (conversation_id, title, ts, ts))
    return conversation_id
