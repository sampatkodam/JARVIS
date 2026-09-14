import subprocess

from app.config import WORKSPACE, COMMAND_TIMEOUT, MAX_OUTPUT_CHARS
from app.tools.shell import run_shell


def git_status() -> dict:
    return run_shell("git status --short --branch")


def git_diff() -> dict:
    return run_shell("git diff --")


def git_log(limit: int = 10) -> dict:
    limit = max(1, min(int(limit), 50))
    return run_shell(f"git log --oneline -{limit}")


def git_branch() -> dict:
    return run_shell("git branch --show-current")


def git_commit(message: str) -> dict:
    """Stage and commit workspace changes without shell interpolation of the message."""
    if not message.strip():
        raise ValueError("Commit message is required.")
    staged = subprocess.run(
        ["git", "add", "-A"], cwd=WORKSPACE, capture_output=True, text=True,
        timeout=COMMAND_TIMEOUT,
    )
    if staged.returncode != 0:
        return {
            "command": "git add -A",
            "return_code": staged.returncode,
            "stdout": staged.stdout[-MAX_OUTPUT_CHARS:],
            "stderr": staged.stderr[-MAX_OUTPUT_CHARS:],
            "success": False,
        }
    committed = subprocess.run(
        ["git", "commit", "-m", message], cwd=WORKSPACE, capture_output=True,
        text=True, timeout=COMMAND_TIMEOUT,
    )
    return {
        "command": "git commit -m <message>",
        "return_code": committed.returncode,
        "stdout": committed.stdout[-MAX_OUTPUT_CHARS:],
        "stderr": committed.stderr[-MAX_OUTPUT_CHARS:],
        "success": committed.returncode == 0,
    }
