from app.capabilities import evaluate
from app.sandbox import run_sandboxed


def run_shell(command: str) -> dict:
    """Run an approved development command inside the OS-level sandbox."""
    allowed, reason, _ = evaluate("run_shell", {"command": command})
    if not allowed:
        raise PermissionError(f"Tool rejected: {reason}")
    return run_sandboxed(command)
