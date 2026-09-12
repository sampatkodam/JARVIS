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
TOOL_DESCRIPTIONS = {name: fn.__doc__ or name for name, fn in TOOLS.items()}
