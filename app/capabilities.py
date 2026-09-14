"""Capability policy for autonomous execution inside the configured workspace."""
from dataclasses import dataclass
import os
import re
import shlex
from pathlib import Path
from typing import Any

from app.config import WORKSPACE

SAFE = "safe"
WORKSPACE_MUTATION = "workspace_mutation"
SYSTEM_LEVEL = "system_level"
BLOCKED = "blocked"

@dataclass(frozen=True)
class Capability:
    name: str
    risk_class: str
    description: str

CAPABILITIES = {
    "list_files": Capability("list_files", SAFE, "List workspace files."),
    "read_file": Capability("read_file", SAFE, "Read workspace files."),
    "search_memory": Capability("search_memory", SAFE, "Search JARVIS memory."),
    "git_status": Capability("git_status", SAFE, "Inspect git status."),
    "git_diff": Capability("git_diff", SAFE, "Inspect git changes."),
    "git_log": Capability("git_log", SAFE, "Inspect git history."),
    "git_branch": Capability("git_branch", SAFE, "Inspect the current branch."),
    "write_file": Capability("write_file", WORKSPACE_MUTATION, "Write workspace files."),
    "run_shell": Capability("run_shell", WORKSPACE_MUTATION, "Run approved workspace commands."),
    "git_commit": Capability("git_commit", WORKSPACE_MUTATION, "Commit workspace changes."),
}

# Commands commonly used by a software agent. Keeping this list explicit makes
# system administration an opt-in architectural decision rather than an accident.
_ALLOWED_EXECUTABLES = {
    "python", "python3", "pip", "pip3", "uv", "poetry", "node", "npm", "npx",
    "pnpm", "yarn", "bun", "deno", "pytest", "git", "cargo", "rustc", "go",
    "java", "javac", "mvn", "gradle", "dotnet", "ruby", "bundle", "php",
    "make", "cmake", "ninja", "ruff", "black", "mypy", "flake8",
    "pytest-asyncio", "coverage", "sqlite3",
}

# Shell control tokens are allowed only when they do not introduce an explicit
# host-level path. This supports normal build/test pipelines while keeping the
# policy independent of the host OS.
_ABSOLUTE = re.compile(r"(?:^|\s)(?:[A-Za-z]:[\\/]|/[A-Za-z0-9_.~-])")
_TRAVERSAL = re.compile(r"(?:^|[\\/])\.\.(?:[\\/]|$)")


def capability(name: str) -> Capability:
    if name not in CAPABILITIES:
        raise PermissionError(f"Unknown capability: {name}")
    return CAPABILITIES[name]


def _tokens(command: str) -> list[str]:
    try:
        return [t.lower() for t in shlex.split(command, posix=(os.name != "nt"))]
    except ValueError:
        return []


def _executable_allowed(token: str) -> bool:
    base = Path(token.replace("\\", "/")).name.lower()
    if base.endswith((".exe", ".cmd", ".bat")):
        base = base.rsplit(".", 1)[0]
    return base in _ALLOWED_EXECUTABLES


def evaluate(tool_name: str, args: dict[str, Any] | None = None) -> tuple[bool, str, Capability]:
    args = args or {}
    cap = capability(tool_name)
    if tool_name in {"read_file", "write_file", "list_files"}:
        raw = str(args.get("path", "."))
        candidate = Path(raw)
        if candidate.is_absolute():
            try:
                resolved = candidate.resolve()
            except OSError:
                return False, "Unable to resolve filesystem path safely.", cap
            if resolved != WORKSPACE and WORKSPACE not in resolved.parents:
                return False, "Filesystem path is outside the configured workspace.", cap
        elif _TRAVERSAL.search(raw):
            return False, "Filesystem path attempts workspace traversal.", cap
    if tool_name == "run_shell":
        command = str(args.get("command", "")).strip()
        tokens = _tokens(command)
        if not command or not tokens:
            return False, "Shell command is empty or malformed.", cap
        if not _executable_allowed(tokens[0]):
            return False, "Executable is outside JARVIS's autonomous workspace capability set.", Capability(tool_name, SYSTEM_LEVEL, cap.description)
        if _TRAVERSAL.search(command):
            return False, "Command contains an explicit workspace traversal path.", cap
        if _ABSOLUTE.search(command):
            return False, "Command contains an explicit absolute host path.", cap
    return True, "Allowed by autonomous workspace policy.", cap
