import json
import uuid
from datetime import datetime, timezone
from app.db import connect
from app.gemini import Gemini
from app.memory import memory_context
from app.tools.registry import TOOLS, TOOL_DESCRIPTIONS, execute_tool

SYSTEM = """You are JARVIS, an autonomous personal software agent.
Work toward the user's goal using available tools. Never invent tool results.
Prefer small, reversible steps. Do not claim completion until verification supports it.
Treat supplied memory as context, not as proof of current state; verify anything that may have changed.
You operate autonomously inside the configured workspace. Use available tools directly; do not ask for routine approval.
Return JSON only when requested."""


def now():
    return datetime.now(timezone.utc).isoformat()


def log_task(task_id: str, message: str, level: str = "info"):
    with connect() as conn:
        conn.execute("INSERT INTO task_logs(task_id, level, message, created_at) VALUES(?,?,?,?)", (task_id, level, str(message)[:20000], now()))


def create_task(goal: str) -> str:
    task_id = str(uuid.uuid4())
    ts = now()
    with connect() as conn:
        conn.execute("INSERT INTO tasks(id, goal, status, created_at, updated_at) VALUES(?,?,?,?,?)", (task_id, goal, "waiting", ts, ts))
    log_task(task_id, "Task queued.")
    return task_id


def get_task(task_id: str):
    with connect() as conn:
        task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not task:
            return None
        steps = conn.execute("SELECT * FROM steps WHERE task_id=? ORDER BY step_no", (task_id,)).fetchall()
        return {"task": dict(task), "steps": [dict(s) for s in steps]}


def get_task_logs(task_id: str, after_id: int = 0, limit: int = 500):
    limit = max(1, min(limit, 1000))
    with connect() as conn:
        rows = conn.execute("SELECT id, task_id, level, message, created_at FROM task_logs WHERE task_id=? AND id>? ORDER BY id ASC LIMIT ?", (task_id, after_id, limit)).fetchall()
    return [dict(r) for r in rows]


def request_pause(task_id: str):
    with connect() as conn:
        row = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row: return None
        status = row["status"]
        if status in ("completed", "failed", "cancelled", "paused"): return status
        if status in ("waiting", "retrying"):
            changed = conn.execute("UPDATE tasks SET status='paused', pause_requested=0, cancel_requested=0, next_run_at=NULL, updated_at=? WHERE id=? AND status IN ('waiting','retrying')", (now(), task_id)).rowcount
            result = "paused" if changed else status
        elif status == "running":
            conn.execute("UPDATE tasks SET pause_requested=1, updated_at=? WHERE id=? AND status='running'", (now(), task_id)); result = "pausing"
        else: result = status
    if result == "paused": log_task(task_id, "Task paused and removed from the runnable queue.", "warning")
    elif result == "pausing": log_task(task_id, "Pause requested; worker will stop at the next safe checkpoint.", "warning")
    return result


def request_cancel(task_id: str):
    with connect() as conn:
        row = conn.execute("SELECT status FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row: return None
        status = row["status"]
        if status in ("completed", "failed", "cancelled"): return status
        if status in ("waiting", "retrying", "paused"):
            changed = conn.execute("UPDATE tasks SET status='cancelled', pause_requested=0, cancel_requested=0, next_run_at=NULL, worker_heartbeat_at=NULL, updated_at=? WHERE id=? AND status IN ('waiting','retrying','paused')", (now(), task_id)).rowcount
            result = "cancelled" if changed else status
        elif status == "running":
            conn.execute("UPDATE tasks SET cancel_requested=1, pause_requested=0, updated_at=? WHERE id=? AND status='running'", (now(), task_id)); result = "cancelling"
        else: result = status
    if result == "cancelled": log_task(task_id, "Task cancelled before further execution.", "warning")
    elif result == "cancelling": log_task(task_id, "Cancellation requested; worker will stop at the next safe checkpoint.", "warning")
    return result


def resume_task(task_id: str):
    with connect() as conn:
        changed = conn.execute("UPDATE tasks SET status='waiting', pause_requested=0, cancel_requested=0, next_run_at=NULL, error=NULL, worker_heartbeat_at=NULL, updated_at=? WHERE id=? AND status='paused'", (now(), task_id)).rowcount
    if changed:
        log_task(task_id, "Task resumed and returned to the runnable queue."); return "waiting"
    record = get_task(task_id)
    return record["task"]["status"] if record else None


def control_state(task_id: str):
    with connect() as conn:
        row = conn.execute("SELECT status, pause_requested, cancel_requested FROM tasks WHERE id=?", (task_id,)).fetchone()
    return dict(row) if row else None


def checkpoint(task_id: str):
    state = control_state(task_id)
    if not state: raise ValueError("Task not found.")
    if state["cancel_requested"]:
        with connect() as conn: conn.execute("UPDATE tasks SET status='cancelled', pause_requested=0, cancel_requested=0, worker_heartbeat_at=NULL, updated_at=? WHERE id=? AND status='running'", (now(), task_id))
        log_task(task_id, "Worker stopped cleanly after cancellation request.", "warning"); return "cancelled"
    if state["pause_requested"]:
        with connect() as conn: conn.execute("UPDATE tasks SET status='paused', pause_requested=0, cancel_requested=0, next_run_at=NULL, worker_heartbeat_at=NULL, updated_at=? WHERE id=? AND status='running'", (now(), task_id))
        log_task(task_id, "Worker paused cleanly at a safe checkpoint.", "warning"); return "paused"
    return state["status"]


def plan(gemini, goal, recovery="", memory=""):
    prompt = f"""Create an executable plan for this goal:
{goal}

Relevant ranked long-term memory:
{memory or 'No relevant long-term memory.'}

Available tools:
{json.dumps(list(TOOL_DESCRIPTIONS.keys()))}

Return exactly:
{{"steps":[{{"description":"...", "tool_name":"exact tool name", "tool_args":{{}}}}]}}

Use at most 6 steps. Treat memory as contextual guidance, not proof of current state. Verify anything that may have changed. {('Recovery context: ' + recovery) if recovery else ''}"""
    return gemini.json(prompt, SYSTEM).get("steps", [])


def critique(gemini, goal, step, output):
    prompt = f"""Evaluate whether this step succeeded in service of the goal.

Goal: {goal}
Step: {json.dumps(step)}
Tool result: {json.dumps(output)[:12000]}

Return:
{{"passed":true/false,"critique":"short explanation"}}"""
    return gemini.json(prompt, SYSTEM)


def _recover_interrupted_steps(task_id: str):
    with connect() as conn:
        rows = conn.execute("SELECT id, step_no, description FROM steps WHERE task_id=? AND status='running' ORDER BY step_no", (task_id,)).fetchall()
        if not rows: return []
        ts = now()
        conn.execute("UPDATE steps SET status='interrupted', critique=?, updated_at=? WHERE task_id=? AND status='running'", ("Worker interrupted while this step was in-flight; tool outcome is unknown and must be verified before retry.", ts, task_id))
    for row in rows: log_task(task_id, f"Recovered interrupted step {row['step_no']}: {row['description']}. Tool outcome is unknown; verification required.", "warning")
    return [dict(row) for row in rows]


def execute_task(task_id: str):
    record = get_task(task_id)
    if not record: raise ValueError("Task not found.")
    if record["task"]["status"] == "cancelled":
        log_task(task_id, "Worker skipped cancelled task.", "warning"); return
    if checkpoint(task_id) != "running": return

    gemini = Gemini(); goal = record["task"]["goal"]; recovery = ""
    interrupted = _recover_interrupted_steps(task_id)
    if interrupted: recovery = f"Recovered {len(interrupted)} interrupted step(s). Their tool outcome is unknown. Verify current state before repeating any potentially side-effecting action."
    log_task(task_id, "Worker started execution.")
    try:
        for cycle in range(1, 13):
            if checkpoint(task_id) != "running": return
            with connect() as conn: conn.execute("UPDATE tasks SET worker_heartbeat_at=?, updated_at=? WHERE id=?", (now(), now(), task_id))
            existing = get_task(task_id)["steps"]
            passed = {s["description"] for s in existing if s["status"] == "passed"}
            log_task(task_id, f"Planning cycle {cycle}.")
            ranked_memory = memory_context(goal, limit=12, scope="task", scope_id=task_id)
            steps = plan(gemini, goal, recovery, ranked_memory)
            if checkpoint(task_id) != "running": return
            selected = next((s for s in steps if s.get("description") not in passed), None)
            if not selected:
                with connect() as conn:
                    conn.execute("UPDATE tasks SET status='completed', result=?, error=NULL, next_run_at=NULL, worker_heartbeat_at=NULL, pause_requested=0, cancel_requested=0, updated_at=? WHERE id=? AND status='running' AND pause_requested=0 AND cancel_requested=0", ("Goal completed and verified by the execution loop.", now(), task_id))
                final = control_state(task_id)
                if final and final["status"] == "completed": log_task(task_id, "Task completed and verified.", "success")
                return

            action = selected.get("tool_name"); args = selected.get("tool_args") or {}; step_no = len(existing) + 1; description = selected.get("description", "")
            log_task(task_id, f"Preparing step {step_no}: {description}"); log_task(task_id, f"Tool: {action or 'reason'}")
            with connect() as conn:
                conn.execute("INSERT INTO steps (task_id,step_no,action,description,status,tool_name,tool_args,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)", (task_id, step_no, action or "reason", description, "running", action, json.dumps(args), now(), now()))
            step_record = get_task(task_id)["steps"][-1]
            if checkpoint(task_id) != "running":
                with connect() as conn: conn.execute("UPDATE steps SET status='interrupted', critique=?, updated_at=? WHERE task_id=? AND step_no=? AND status='running'", ("Control request arrived before tool invocation; step was not executed.", now(), task_id, step_no))
                return

            if action not in TOOLS:
                output = {"success": False, "error": f"Unknown tool: {action}"}
            else:
                try:
                    output = execute_tool(action, args, task_id=task_id, step_id=step_record["id"])
                except Exception as exc:
                    output = {"success": False, "error": str(exc)}

            if checkpoint(task_id) != "running":
                with connect() as conn: conn.execute("UPDATE steps SET status='interrupted', output=?, critique=?, updated_at=? WHERE task_id=? AND step_no=? AND status='running'", (json.dumps(output)[:20000], "Tool returned, but task control changed before verification; outcome must be reviewed on resume.", now(), task_id, step_no))
                log_task(task_id, "Current tool call finished; worker will not continue to another step.", "warning"); return

            log_task(task_id, f"Tool result: {json.dumps(output)[:12000]}", "error" if output.get("success") is False else "info")
            result = critique(gemini, goal, selected, output)
            if checkpoint(task_id) != "running":
                with connect() as conn: conn.execute("UPDATE steps SET status='interrupted', output=?, critique=?, updated_at=? WHERE task_id=? AND step_no=? AND status='running'", (json.dumps(output)[:20000], "Tool result exists but verification was interrupted; outcome must be reviewed on resume.", now(), task_id, step_no))
                return
            status = "passed" if result.get("passed") else "failed"
            with connect() as conn: conn.execute("UPDATE steps SET status=?, output=?, critique=?, updated_at=? WHERE task_id=? AND step_no=? AND status='running'", (status, json.dumps(output)[:20000], json.dumps(result)[:10000], now(), task_id, step_no))
            log_task(task_id, f"Step {step_no} {status}: {result.get('critique', '')}", "success" if status == "passed" else "warning")
            recovery = "" if status == "passed" else result.get("critique", "Step failed.")
        raise RuntimeError("Maximum autonomous execution cycles reached.")
    except Exception as exc:
        state = control_state(task_id)
        if state and state["status"] in ("paused", "cancelled"): return
        with connect() as conn: conn.execute("UPDATE tasks SET status='failed', error=?, worker_heartbeat_at=NULL, updated_at=? WHERE id=?", (str(exc), now(), task_id))
        log_task(task_id, f"Execution failed: {exc}", "error")
        raise
