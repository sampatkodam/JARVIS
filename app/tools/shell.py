import subprocess

from app.capabilities import evaluate
from app.config import WORKSPACE, COMMAND_TIMEOUT, MAX_OUTPUT_CHARS


def run_shell(command: str) -> dict:
    """Run an approved development command from the configured workspace."""
    allowed, reason, _ = evaluate("run_shell", {"command": command})
    if not allowed:
        raise PermissionError(f"Tool rejected: {reason}")
    completed = subprocess.run(
        command,
        cwd=WORKSPACE,
        shell=True,
        capture_output=True,
        text=True,
        timeout=COMMAND_TIMEOUT,
    )
    return {
        "command": command,
        "return_code": completed.returncode,
        "stdout": completed.stdout[-MAX_OUTPUT_CHARS:],
        "stderr": completed.stderr[-MAX_OUTPUT_CHARS:],
        "success": completed.returncode == 0,
    }
