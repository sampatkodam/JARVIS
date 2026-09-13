import json
import math
import re
import uuid
from datetime import datetime, timezone
from app.config import WORKSPACE
from app.db import connect
from app.gemini import Gemini

MEMORY_SYSTEM = """You are JARVIS memory extraction. Extract only durable, useful facts supported by the supplied material.
Do not invent facts. Do not store secrets, API keys, passwords, tokens, or transient chatter.
Return JSON only: {\"memories\":[{\"kind\":\"preference|fact|decision|constraint|project|workspace|lesson\",\"key\":\"short stable key\",\"content\":\"concise fact\",\"confidence\":0.0}]}
Prefer high-value facts that help future tasks. If nothing durable is present, return {\"memories\":[]}."""

EMBEDDING_MODEL = "gemini-embedding-001"
SCOPE_WEIGHTS = {"task": 1.00, "project": 0.96, "workspace": 0.90, "conversation": 0.84, "global": 0.78}
RECENCY_HALF_LIFE_DAYS = 30.0


def now(): return datetime.now(timezone.utc).isoformat()
def workspace_id(): return str(WORKSPACE)


def _embedding_text(kind, key, content):
    return f"{kind}: {key}\n{content}"[:12000]


def _pack_embedding(values):
    return json.dumps([float(v) for v in values], separators=(",", ":")).encode("utf-8")


def _unpack_embedding(blob):
    if not blob: return None
    try: return [float(v) for v in json.loads(bytes(blob).decode("utf-8"))]
    except (ValueError, TypeError, UnicodeDecodeError, json.JSONDecodeError): return None


def _cosine(a, b):
    if not a or not b or len(a) != len(b): return 0.0
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)); nb = math.sqrt(sum(y * y for y in b))
    if not na or not nb: return 0.0
    return max(0.0, min(1.0, dot / (na * nb)))


def _recency_score(updated_at):
    try: age = max(0.0, (datetime.now(timezone.utc) - datetime.fromisoformat(updated_at)).total_seconds() / 86400.0)
    except (TypeError, ValueError): return 0.0
    return math.exp(-math.log(2) * age / RECENCY_HALF_LIFE_DAYS)


def _lexical_score(query, row):
    q = set(re.findall(r"[a-z0-9_]+", str(query).lower()))
    if not q: return 0.0
    text = set(re.findall(r"[a-z0-9_]+", f"{row['key']} {row['content']} {row['kind']}".lower()))
    return len(q & text) / len(q)


def upsert_memory(scope, scope_id, kind, key, content, source_type="manual", source_id=None, confidence=1.0):
    content,key=str(content).strip()[:12000],str(key).strip()[:200]
    if not content or not key:return None
    confidence=max(0.0,min(1.0,float(confidence)))
    embedding_blob, embedding_model = None, None
    try:
        values = Gemini().embed(_embedding_text(kind, key, content))
        if values: embedding_blob, embedding_model = _pack_embedding(values), EMBEDDING_MODEL
    except Exception:
        pass
    with connect() as conn:
        ts=now()
        conn.execute("""INSERT INTO memories(scope,scope_id,kind,key,content,source_type,source_id,confidence,embedding,embedding_model,created_at,updated_at)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(scope,scope_id,kind,key) DO UPDATE SET content=excluded.content,source_type=excluded.source_type,source_id=excluded.source_id,confidence=excluded.confidence,embedding=COALESCE(excluded.embedding,memories.embedding),embedding_model=COALESCE(excluded.embedding_model,memories.embedding_model),updated_at=excluded.updated_at""",(scope,scope_id,kind,key,content,source_type,source_id,confidence,embedding_blob,embedding_model,ts,ts))
        row=conn.execute("SELECT id FROM memories WHERE scope=? AND scope_id IS ? AND kind=? AND key=?",(scope,scope_id,kind,key)).fetchone(); return row["id"] if row else None


def add_message(conversation_id, role, content, task_id=None):
    ts=now()
    with connect() as conn:
        conn.execute("INSERT OR IGNORE INTO conversations(id,title,created_at,updated_at) VALUES(?,?,?,?)",(conversation_id,None,ts,ts)); conn.execute("INSERT INTO messages(conversation_id,role,content,task_id,created_at) VALUES(?,?,?,?,?)",(conversation_id,role,str(content)[:50000],task_id,ts)); conn.execute("UPDATE conversations SET updated_at=? WHERE id=?",(ts,conversation_id))


def conversation_history(conversation_id, limit=40):
    limit=max(1,min(int(limit),200))
    with connect() as conn: rows=conn.execute("SELECT id,conversation_id,role,content,task_id,created_at FROM messages WHERE conversation_id=? ORDER BY id DESC LIMIT ?",(conversation_id,limit)).fetchall()
    return [dict(r) for r in reversed(rows)]


def task_history(limit=30):
    limit=max(1,min(int(limit),100))
    with connect() as conn: rows=conn.execute("SELECT id,goal,status,result,error,retry_count,created_at,updated_at FROM tasks ORDER BY created_at DESC LIMIT ?",(limit,)).fetchall()
    return [dict(r) for r in rows]


def _candidate_memories(query, scopes=None):
    scope_clause = " AND scope IN (" + ",".join("?" for _ in scopes) + ")" if scopes else ""
    params=list(scopes or [])
    with connect() as conn:
        rows=conn.execute(f"SELECT id,scope,scope_id,kind,key,content,source_type,source_id,confidence,embedding,embedding_model,created_at,updated_at FROM memories WHERE 1=1{scope_clause} ORDER BY updated_at DESC LIMIT 500",params).fetchall()
    return [dict(r) for r in rows]


def _scope_score(row_scope, requested_scope=None, requested_scope_id=None, row_scope_id=None):
    score=SCOPE_WEIGHTS.get(row_scope,0.70)
    if requested_scope and row_scope == requested_scope:
        score=1.0
    if requested_scope_id is not None and row_scope_id == requested_scope_id:
        score=1.0
    return score


def _rank_memories(query, rows, limit=20, scope=None, scope_id=None):
    if not rows: return []
    query_embedding=None
    try: query_embedding=Gemini().embed(str(query)[:12000])
    except Exception: pass
    ranked=[]
    for row in rows:
        lexical=_lexical_score(query,row)
        semantic=_cosine(query_embedding,_unpack_embedding(row.get("embedding"))) if query_embedding else 0.0
        # Lexical matching is a fallback/boost, never a substitute that creates semantic similarity.
        if not query_embedding or not row.get("embedding"):
            semantic=lexical
        else:
            semantic=max(semantic, lexical * 0.75)
        scope_score=_scope_score(row["scope"],scope,scope_id,row.get("scope_id"))
        recency=_recency_score(row["updated_at"])
        confidence=max(0.0,min(1.0,float(row["confidence"] or 0.0)))
        total=0.55*semantic + 0.20*scope_score + 0.15*recency + 0.10*confidence
        row["lexical_score"]=round(lexical,4); row["semantic_similarity"]=round(semantic,4); row["scope_score"]=round(scope_score,4); row["recency_score"]=round(recency,4); row["relevance_score"]=round(total,4)
        ranked.append(row)
    ranked.sort(key=lambda r:(r["relevance_score"],r["semantic_similarity"],r["updated_at"]),reverse=True)
    return ranked[:max(1,min(int(limit),100))]


def search_memories(query, limit=20, scopes=None, scope=None, scope_id=None):
    query=str(query).strip()
    if not query:return []
    rows=_candidate_memories(query,scopes)
    return _rank_memories(query,rows,limit,scope,scope_id)


def search_memory_tool(query: str, limit: int = 8) -> dict:
    """Search JARVIS persistent memory using semantic similarity, scope relevance, recency, and confidence."""
    return {"query":query,"memories":search_memories(query,limit)}


def memory_context(query, limit=12, scope=None, scope_id=None):
    rows=search_memories(query,limit,scope=scope,scope_id=scope_id)
    if not rows:return "No matching long-term memories."
    return "\n".join(f"[{r['relevance_score']:.3f}] [{r['scope']}] {r['kind']}/{r['key']}: {r['content']}" for r in rows)


def extract_memories(text, scope="global", scope_id=None, source_type="conversation", source_id=None):
    text=str(text).strip()
    if not text:return []
    try: result=Gemini().json("Extract durable memories from this material:\n\n"+text[:30000],MEMORY_SYSTEM)
    except Exception:return []
    saved=[]
    for item in result.get("memories",[]) if isinstance(result,dict) else []:
        if not isinstance(item,dict):continue
        kind,key,content=item.get("kind","fact"),item.get("key"),item.get("content")
        if key and content and not re.search(r"(?i)(api[_ -]?key|password|secret|token|private[_ -]?key)",f"{key} {content}"):
            mid=upsert_memory(scope,scope_id,kind,key,content,source_type,source_id,item.get("confidence",0.7))
            if mid:saved.append(mid)
    return saved


def extract_task_memory(task_id):
    with connect() as conn:
        task=conn.execute("SELECT id,goal,status,result,error FROM tasks WHERE id=?",(task_id,)).fetchone(); steps=conn.execute("SELECT description,status,output,critique FROM steps WHERE task_id=? ORDER BY step_no",(task_id,)).fetchall()
    if not task or task["status"] not in ("completed","failed"):return []
    return extract_memories(json.dumps(dict(task),ensure_ascii=False)+"\n"+json.dumps([dict(s) for s in steps],ensure_ascii=False),scope="task",scope_id=task_id,source_type="task",source_id=task_id)


def extract_workspace_memory():
    chunks=[]
    for path in sorted(WORKSPACE.rglob("*")):
        if not path.is_file() or path.stat().st_size>30000 or path.suffix.lower() not in {".md",".txt",".json",".yaml",".yml",".toml",".py",".js",".ts"}:continue
        try:chunks.append(f"FILE: {path.relative_to(WORKSPACE)}\n{path.read_text(encoding='utf-8')[:12000]}")
        except (OSError,UnicodeDecodeError):continue
        if len(chunks)>=12:break
    if not chunks:return []
    return extract_memories("Workspace evidence:\n"+"\n\n".join(chunks),scope="workspace",scope_id=workspace_id(),source_type="workspace",source_id=workspace_id())


def new_conversation(title=None):
    conversation_id=str(uuid.uuid4()); ts=now()
    with connect() as conn:conn.execute("INSERT INTO conversations(id,title,created_at,updated_at) VALUES(?,?,?,?)",(conversation_id,title,ts,ts))
    return conversation_id
