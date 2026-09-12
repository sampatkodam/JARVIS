from pathlib import Path
import os
from dotenv import load_dotenv

load_dotenv()
BASE_DIR = Path(__file__).resolve().parent.parent
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "").strip()
DB_PATH = Path(os.getenv("JARVIS_DB_PATH", str(BASE_DIR / "data" / "jarvis.db"))).resolve()
WORKSPACE = Path(os.getenv("JARVIS_WORKSPACE", str(BASE_DIR / "workspace"))).resolve()
COMMAND_TIMEOUT = int(os.getenv("JARVIS_COMMAND_TIMEOUT", "60"))
MAX_OUTPUT_CHARS = int(os.getenv("JARVIS_MAX_OUTPUT_CHARS", "12000"))
DB_PATH.parent.mkdir(parents=True, exist_ok=True)
WORKSPACE.mkdir(parents=True, exist_ok=True)
