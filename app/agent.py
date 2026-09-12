import json
import uuid
from datetime import datetime, timezone
from app.db import connect
from app.gemini import Gemini
from app.tools.registry import TOOLS, TOOL_DESCRIPTIONS

SYSTEM = """You are JARVIS, an autonomous personal software agent.
Work toward the user's goal using available tools. Never invent tool results.
Prefer small, reversible steps. Do not claim completion until verification supports it.
Return JSON only when requested."""

def now():
    return datetime.now(timezone.utc).isoformat()

def create_task(goal: str) -> str:
    task_id = str(uuid.uuid4())
    ts = now()
    with connect() as conn:
        conn.execute(
            "INSERT INTO tasks(id, goal, status, created_at, updated_at) VALUES(?,?,?,?,?)",
            (task_id, goal, "queued", ts, ts))
    return task_id

def get_task(task_id: str):
    with connect() as conn:
        task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not task:
            return None
        steps = conn.execute(
            "SELECT * FROM steps WHERE task_id=? ORDER BY step_no", (task_id,)).fetchall()
        return {"task": dict(task), "steps": [dict(s) for s in steps]}

def cancel_task(task_id: str):
    with connect() as conn:
        conn.execute("UPDATE tasks SET status=?, updated_at=? WHERE id=?",
                     ("cancelled", now(), task_id))

def plan(gemini, goal, recovery=""):
    prompt = f"""Create an executable plan for this goal:
{goal}

Available tools:
{json.dumps(list(TOOL_DESCRIPTIONS.keys()))}

Return exactly:
{{"steps":[{{"description":"...", "tool_name":"exact tool name", "tool_args":{{}}}}]}}

Use at most 6 steps. {("Recovery context: " + recovery) if recovery else ""}"""
    return gemini.json(prompt, SYSTEM).get("steps", [])

def critique(gemini, goal, step, output):
    prompt = f"""Evaluate whether this step succeeded in service of the goal.

Goal: {goal}
Step: {json.dumps(step)}
Tool result: {json.dumps(output)[:12000]}

Return:
{{"passed":true/false,"critique":"short explanation"}}"""
    return gemini.json(prompt, SYSTEM)

def run_task(task_id: str):
    record = get_task(task_id)
    if not record:
        raise ValueError("Task not found.")
    if record["task"]["status"] == "cancelled":
        return

    gemini = Gemini()
    goal = record["task"]["goal"]
    with connect() as conn:
        conn.execute("UPDATE tasks SET status=?, updated_at=? WHERE id=?",
                     ("running", now(), task_id))

    recovery = ""
    try:
        for _ in range(12):
            existing = get_task(task_id)["steps"]
            passed = {s["description"] for s in existing if s["status"] == "passed"}
            steps = plan(gemini, goal, recovery)
            selected = next((s for s in steps if s.get("description") not in passed), None)

            if not selected:
                with connect() as conn:
                    conn.execute(
                        "UPDATE tasks SET status=?, result=?, updated_at=? WHERE id=?",
                        ("completed", "Goal completed and verified by the execution loop.",
                         now(), task_id))
                return

            action = selected.get("tool_name")
            args = selected.get("tool_args") or {}
            if action not in TOOLS:
                output = {"success": False, "error": f"Unknown tool: {action}"}
            else:
                try:
                    output = TOOLS[action](**args)
                except Exception as exc:
                    output = {"success": False, "error": str(exc)}

            result = critique(gemini, goal, selected, output)
            status = "passed" if result.get("passed") else "failed"
            step_no = len(existing) + 1

            with connect() as conn:
                conn.execute(
                    """INSERT INTO steps
                    (task_id,step_no,action,description,status,tool_name,tool_args,output,critique,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (task_id, step_no, action or "reason",
                     selected.get("description",""), status, action,
                     json.dumps(args), json.dumps(output)[:20000],
                     json.dumps(result)[:10000], now(), now()))

            recovery = "" if status == "passed" else result.get("critique", "Step failed.")

        raise RuntimeError("Maximum autonomous execution cycles reached.")
    except Exception as exc:
        with connect() as conn:
            conn.execute("UPDATE tasks SET status=?, error=?, updated_at=? WHERE id=?",
                         ("failed", str(exc), now(), task_id))
        raise
