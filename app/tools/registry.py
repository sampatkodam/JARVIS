import json
from datetime import datetime, timezone

from app.db import connect
from app.capabilities import CAPABILITIES, evaluate
from app.tools.fs import list_files, read_file, write_file
from app.tools.shell import run_shell
from app.tools.git import git_status, git_diff, git_log, git_branch, git_commit
from app.memory import search_memory_tool

TOOLS = {
    "list_files": list_files,
    "read_file": read_file,
    "write_file": write_file,
    "run_shell": run_shell,
    "git_status": git_status,
    "git_diff": git_diff,
    "git_log": git_log,
    "git_branch": git_branch,
    "git_commit": git_commit,
    "search_memory": search_memory_tool,
}
TOOL_DESCRIPTIONS = {name: CAPABILITIES[name].description for name in TOOLS}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _audit(task_id, step_id, tool_name, risk_class, decision, reason, args, result=None):
    summary = None if result is None else str(result)[:4000]
    with connect() as conn:
        conn.execute(
            "INSERT INTO tool_audit (task_id,step_id,tool_name,risk_class,decision,reason,arguments,result_summary,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (task_id, step_id, tool_name, risk_class, decision, reason, json.dumps(args, default=str)[:12000], summary, _now()),
        )


def execute_tool(tool_name, args=None, task_id=None, step_id=None):
    """Central autonomous execution gateway."""
    args = args or {}
    allowed, reason, cap = evaluate(tool_name, args)
    if not allowed:
        _audit(task_id, step_id, tool_name, cap.risk_class, "denied", reason, args)
        return {"success": False, "error": f"Tool rejected: {reason}"}
    _audit(task_id, step_id, tool_name, cap.risk_class, "allowed", reason, args)
    try:
        result = TOOLS[tool_name](**args)
    except Exception as exc:
        _audit(task_id, step_id, tool_name, cap.risk_class, "error", "Tool raised an exception after capability evaluation.", args, exc)
        raise
    _audit(task_id, step_id, tool_name, cap.risk_class, "completed", "Tool completed after capability evaluation.", args, result)
    return result
