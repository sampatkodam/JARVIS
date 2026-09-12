from pathlib import Path
from app.config import WORKSPACE

def safe_path(relative: str) -> Path:
    candidate = (WORKSPACE / relative).resolve()
    if candidate != WORKSPACE and WORKSPACE not in candidate.parents:
        raise ValueError("Path escapes the configured JARVIS workspace.")
    return candidate

def list_files(path: str = ".") -> dict:
    target = safe_path(path)
    if not target.is_dir():
        raise ValueError("Path is not a directory.")
    return {"path": path, "items": [
        {"name": p.name, "type": "directory" if p.is_dir() else "file"}
        for p in sorted(target.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower()))
    ]}

def read_file(path: str) -> dict:
    target = safe_path(path)
    if not target.is_file():
        raise FileNotFoundError(path)
    return {"path": path, "content": target.read_text(encoding="utf-8")}

def write_file(path: str, content: str) -> dict:
    target = safe_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return {"path": path, "bytes": target.stat().st_size}
