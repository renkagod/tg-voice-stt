import os
import tempfile
from dotenv import load_dotenv

# Load .env file
load_dotenv()

# Required environment variables
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY")
ALLOWED_USERS_RAW = os.getenv("ALLOWED_USERS")

# Validate required configuration
missing_vars = []
if not TELEGRAM_TOKEN or TELEGRAM_TOKEN.strip() == "your_telegram_bot_token_here":
    missing_vars.append("TELEGRAM_TOKEN")

# ALLOWED_USERS are the administrators who have access without submitting their own key
ADMIN_USERS = set()
ALLOWED_USERS = ADMIN_USERS  # alias for backwards compatibility
if ALLOWED_USERS_RAW:
    for user_id in ALLOWED_USERS_RAW.split(","):
        try:
            ADMIN_USERS.add(int(user_id.strip()))
        except ValueError:
            print(f"Warning: Invalid user ID '{user_id}' in ALLOWED_USERS. Skipping.")

# Models configuration (purely from .env)
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.5-flash-lite").strip()
DEFAULT_GEMINI_MODEL = GEMINI_MODEL
GEMINI_FALLBACK_MODEL = os.getenv("GEMINI_FALLBACK_MODEL", "gemini-3.5-flash-lite").strip()
GEMINI_SUMMARY_MODEL = os.getenv("GEMINI_SUMMARY_MODEL", "gemini-3.5-flash-lite").strip()

# Database path for SQLite key pool with write permission validation
def _is_dir_writable(path: str) -> bool:
    try:
        os.makedirs(path, exist_ok=True)
        test_file = os.path.join(path, ".test_write")
        with open(test_file, "w") as f:
            f.write("1")
        os.remove(test_file)
        return True
    except Exception:
        return False

def _get_default_db_path() -> str:
    env_path = os.getenv("DB_PATH")
    if env_path:
        d = os.path.dirname(os.path.abspath(env_path))
        if _is_dir_writable(d):
            return env_path

    # Check /app/data or local data/
    data_dir = os.path.join(os.path.dirname(__file__), "data")
    if _is_dir_writable(data_dir):
        return os.path.join(data_dir, "keys.db")

    # Fallback to temp directory (guaranteed writable tmpfs in Docker)
    return os.path.join(tempfile.gettempdir(), "keys.db")

DB_PATH = _get_default_db_path()
