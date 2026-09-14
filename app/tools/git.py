import shlex

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
    """Stage and commit workspace changes entirely inside the OS sandbox."""
    if not message.strip():
        raise ValueError("Commit message is required.")
    return run_shell(f"git add -A && git commit -m {shlex.quote(message)}")
