import subprocess
from app.config import WORKSPACE, COMMAND_TIMEOUT, MAX_OUTPUT_CHARS

BLOCKED = [
    "format ", "diskpart", "shutdown", "reboot", "rm -rf /",
    "del /s /q c:\\", "rmdir /s /q c:\\",
]

def run_shell(command: str) -> dict:
    lowered = command.lower().strip()
    if any(token in lowered for token in BLOCKED):
        raise PermissionError("Command blocked by JARVIS V0.1 safety policy.")
    completed = subprocess.run(
        command, cwd=WORKSPACE, shell=True, capture_output=True,
        text=True, timeout=COMMAND_TIMEOUT
    )
    return {
        "command": command,
        "return_code": completed.returncode,
        "stdout": completed.stdout[-MAX_OUTPUT_CHARS:],
        "stderr": completed.stderr[-MAX_OUTPUT_CHARS:],
        "success": completed.returncode == 0,
    }
