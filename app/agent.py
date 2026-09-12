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


def log_task(task_id: str, message: str, level: str = "info"):
    with connect() as conn:
        conn.execute(
            "INSERT INTO task_logs(task_id, level, message, created_at) VALUES(?,?,?,?)",
            (task_id, level, str(message)[:20000], now()),
        )


def create_task(goal: str) -> str:
    task_id = str(uuid.uuid4())
    ts = now()
    with connect() as conn:
        conn.execute(
            "INSERT INTO tasks(id, goal, status, created_at, updated_at) VALUES(?,?,?,?,?)",
            (task_id, goal, "waiting", ts, ts),
        )
    log_task(task_id, "Task queued.")
    return task_id


def get_task(task_id: str):
    with connect() as conn:
        task = conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not task:
            return None
        steps = conn.execute(
            "SELECT * FROM steps WHERE task_id=? ORDER BY step_no", (task_id,)
        ).fetchall()
        return {"task": dict(task), "steps": [dict(s) for s in steps]}


def get_task_logs(task_id: str, after_id: int = 0, limit: int = 500):
    limit = max(1, min(limit, 1000))
    with connect() as conn:
        rows = conn.execute(
            """SELECT id, task_id, level, message, created_at
               FROM task_logs WHERE task_id=? AND id>?
               ORDER BY id ASC LIMIT ?""",
            (task_id, after_id, limit),
        ).fetchall()
    return [dict(r) for r in rows]


def cancel_task(task_id: str):
    with connect() as conn:
        changed = conn.execute(
            """UPDATE tasks SET status='cancelled', updated_at=?
               WHERE id=? AND status IN ('waiting','retrying','running')""",
            (now(), task_id),
        ).rowcount
    if changed:
        log_task(task_id, "Task cancellation requested.", "warning")


def plan(gemini, goal, recovery=""):
    prompt = f"""Create an executable plan for this goal:
{goal}

Available tools:
{json.dumps(list(TOOL_DESCRIPTIONS.keys()))}

Return exactly:
{{"steps":[{{"description":"...", "tool_name":"exact tool name", "tool_args":{{}}}}]}}

Use at most 6 steps. {('Recovery context: ' + recovery) if recovery else ''}"""
    return gemini.json(prompt, SYSTEM).get("steps", [])


def critique(gemini, goal, step, output):
    prompt = f"""Evaluate whether this step succeeded in service of the goal.

Goal: {goal}
Step: {json.dumps(step)}
Tool result: {json.dumps(output)[:12000]}

Return:
{{"passed":true/false,"critique":"short explanation"}}"""
    return gemini.json(prompt, SYSTEM)


def execute_task(task_id: str):
    """Execute one claimed queue item. The worker owns the running transition."""
    record = get_task(task_id)
    if not record:
        raise ValueError("Task not found.")
    if record["task"]["status"] == "cancelled":
        log_task(task_id, "Worker skipped cancelled task.", "warning")
        return

    gemini = Gemini()
    goal = record["task"]["goal"]
    recovery = ""
    log_task(task_id, "Worker started execution.")
    try:
        for cycle in range(1, 13):
            with connect() as conn:
                conn.execute(
                    "UPDATE tasks SET worker_heartbeat_at=?, updated_at=? WHERE id=?",
                    (now(), now(), task_id),
                )
            existing = get_task(task_id)["steps"]
            passed = {s["description"] for s in existing if s["status"] == "passed"}
            log_task(task_id, f"Planning cycle {cycle}.")
            steps = plan(gemini, goal, recovery)
            selected = next((s for s in steps if s.get("description") not in passed), None)

            if not selected:
                with connect() as conn:
                    conn.execute(
                        """UPDATE tasks SET status='completed', result=?, error=NULL,
                           next_run_at=NULL, worker_heartbeat_at=NULL, updated_at=? WHERE id=?""",
                        ("Goal completed and verified by the execution loop.", now(), task_id),
                    )
                log_task(task_id, "Task completed and verified.", "success")
                return

            action = selected.get("tool_name")
            args = selected.get("tool_args") or {}
            log_task(task_id, f"Executing step {len(existing) + 1}: {selected.get('description', '')}")
            log_task(task_id, f"Tool: {action or 'reason'}")
            if action not in TOOLS:
                output = {"success": False, "error": f"Unknown tool: {action}"}
            else:
                try:
                    output = TOOLS[action](**args)
                except Exception as exc:
                    output = {"success": False, "error": str(exc)}

            log_task(task_id, f"Tool result: {json.dumps(output)[:12000]}", "error" if output.get("success") is False else "info")
            result = critique(gemini, goal, selected, output)
            status = "passed" if result.get("passed") else "failed"
            step_no = len(existing) + 1
            with connect() as conn:
                conn.execute(
                    """INSERT INTO steps
                    (task_id,step_no,action,description,status,tool_name,tool_args,output,critique,created_at,updated_at)
                    VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (task_id, step_no, action or "reason", selected.get("description", ""),
                     status, action, json.dumps(args), json.dumps(output)[:20000],
                     json.dumps(result)[:10000], now(), now()),
                )
            log_task(task_id, f"Step {step_no} {status}: {result.get('critique', '')}", "success" if status == "passed" else "warning")
            recovery = "" if status == "passed" else result.get("critique", "Step failed.")

        raise RuntimeError("Maximum autonomous execution cycles reached.")
    except Exception as exc:
        with connect() as conn:
            conn.execute(
                "UPDATE tasks SET status='failed', error=?, worker_heartbeat_at=NULL, updated_at=? WHERE id=?",
                (str(exc), now(), task_id),
            )
        log_task(task_id, f"Execution failed: {exc}", "error")
        raise
