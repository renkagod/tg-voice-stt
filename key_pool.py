import os
import time
import sqlite3
import logging
import aiohttp
from typing import Optional, Tuple, List, Dict, Any, Set
from contextlib import contextmanager

logger = logging.getLogger(__name__)

# Default DB path if not set by config
DEFAULT_DB_PATH = os.path.join(os.path.dirname(__file__), "data", "keys.db")

def get_db_path() -> str:
    from config import DB_PATH
    return DB_PATH or DEFAULT_DB_PATH

_fallback_db_path: Optional[str] = None

@contextmanager
def get_db():
    global _fallback_db_path
    db_path = _fallback_db_path or get_db_path()
    db_dir = os.path.dirname(db_path)
    if db_dir and not os.path.exists(db_dir):
        try:
            os.makedirs(db_dir, exist_ok=True)
        except Exception as e:
            logger.warning(f"Could not create database directory {db_dir}: {e}")
    try:
        conn = sqlite3.connect(db_path)
    except sqlite3.OperationalError as e:
        import tempfile
        _fallback_db_path = os.path.join(tempfile.gettempdir(), "keys.db")
        logger.warning(f"Could not open SQLite database at '{db_path}': {e}. Falling back to '{_fallback_db_path}'.")
        conn = sqlite3.connect(_fallback_db_path)

    conn.row_factory = sqlite3.Row
    try:
        yield conn
    finally:
        conn.close()

def init_pool(default_env_key: Optional[str] = None):
    """
    Initializes the SQLite schema and seeds the default system key if provided.
    """
    with get_db() as conn:
        conn.execute("""
            CREATE TABLE IF NOT EXISTS api_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id INTEGER NOT NULL,
                api_key TEXT UNIQUE NOT NULL,
                is_active INTEGER DEFAULT 1,
                cooldown_until REAL DEFAULT 0,
                last_used_at REAL DEFAULT 0,
                created_at REAL DEFAULT (strftime('%s', 'now'))
            )
        """)
        conn.execute("CREATE INDEX IF NOT EXISTS idx_active_keys ON api_keys(is_active, cooldown_until, last_used_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_user_keys ON api_keys(user_id, is_active)")
        conn.commit()

        if default_env_key and default_env_key.strip():
            clean_key = default_env_key.strip()
            # Insert system key with user_id = 0 if not already in DB
            cur = conn.execute("SELECT id FROM api_keys WHERE api_key = ?", (clean_key,))
            if not cur.fetchone():
                conn.execute(
                    "INSERT INTO api_keys (user_id, api_key, is_active, cooldown_until, last_used_at) VALUES (0, ?, 1, 0, 0)",
                    (clean_key,)
                )
                conn.commit()
                logger.info("Base GEMINI_API_KEY from .env seeded into pool (user_id=0).")

async def validate_key_online(api_key: str) -> Tuple[bool, str]:
    """
    Validates a Google Gemini API key by making a lightweight GET request to models list.
    """
    url = f"https://generativelanguage.googleapis.com/v1beta/models?key={api_key}"
    timeout = aiohttp.ClientTimeout(total=10)
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.get(url) as response:
                if response.status == 200:
                    return True, "OK"
                error_body = await response.text()
                logger.warning(f"Key validation failed (Status {response.status}): {error_body}")
                if response.status in (400, 403):
                    return False, "Неверный или заблокированный API-ключ."
                return False, f"Ошибка проверки Google API (код {response.status})."
    except Exception as e:
        logger.error(f"Key validation connection error: {e}")
        return False, "Не удалось подключиться к серверам Google для проверки ключа."

async def add_user_key(user_id: int, raw_key: str) -> Tuple[bool, str]:
    """
    Checks for duplicate, validates online, and adds key to pool.
    """
    api_key = raw_key.strip()
    if not api_key:
        return False, "Ключ не может быть пустым."

    # 1. Check duplicate in database
    with get_db() as conn:
        cur = conn.execute("SELECT user_id, is_active FROM api_keys WHERE api_key = ?", (api_key,))
        row = cur.fetchone()
        if row:
            return False, "Этот API-ключ уже зарегистрирован в базе."

    # 2. Online validation
    is_valid, err_msg = await validate_key_online(api_key)
    if not is_valid:
        return False, f"Ключ не прошел проверку: {err_msg}"

    # 3. Save key to database
    with get_db() as conn:
        conn.execute(
            "INSERT INTO api_keys (user_id, api_key, is_active, cooldown_until, last_used_at) VALUES (?, ?, 1, 0, 0)",
            (user_id, api_key)
        )
        conn.commit()

    logger.info(f"User {user_id} successfully added a new key to the pool.")
    return True, "Ключ успешно проверен и добавлен в общую казну! Доступ к боту активирован."

def get_active_key() -> Optional[str]:
    """
    Retrieves the least-recently used active key that is not in cooldown (Round-Robin).
    Updates last_used_at timestamp.
    """
    now = time.time()
    with get_db() as conn:
        cur = conn.execute("""
            SELECT id, api_key FROM api_keys 
            WHERE is_active = 1 AND cooldown_until <= ? 
            ORDER BY last_used_at ASC 
            LIMIT 1
        """, (now,))
        row = cur.fetchone()
        if not row:
            return None
        key_id = row["id"]
        api_key = row["api_key"]
        conn.execute("UPDATE api_keys SET last_used_at = ? WHERE id = ?", (now, key_id))
        conn.commit()
        return api_key

def put_on_cooldown(api_key: str, seconds: int = 60):
    """
    Temporarily puts a key on cooldown due to rate limits (429).
    """
    until = time.time() + seconds
    with get_db() as conn:
        conn.execute("UPDATE api_keys SET cooldown_until = ? WHERE api_key = ?", (until, api_key))
        conn.commit()
    logger.info(f"Key ...{api_key[-6:] if len(api_key) > 6 else 'key'} placed on cooldown for {seconds}s.")

def revoke_key(api_key: str, reason: str = "") -> Optional[int]:
    """
    Deactivates a dead key (400/403) and returns owner's user_id.
    """
    with get_db() as conn:
        cur = conn.execute("SELECT user_id FROM api_keys WHERE api_key = ?", (api_key,))
        row = cur.fetchone()
        if not row:
            return None
        user_id = row["user_id"]
        conn.execute("UPDATE api_keys SET is_active = 0 WHERE api_key = ?", (api_key,))
        conn.commit()
    logger.warning(f"Key ...{api_key[-6:] if len(api_key) > 6 else 'key'} revoked (user_id={user_id}). Reason: {reason}")
    return user_id

def revoke_user_keys(user_id: int) -> int:
    """
    Revokes all active keys for a specific user. Returns count of revoked keys.
    """
    with get_db() as conn:
        cur = conn.execute("UPDATE api_keys SET is_active = 0 WHERE user_id = ? AND is_active = 1", (user_id,))
        conn.commit()
        count = cur.rowcount
    logger.info(f"Revoked {count} keys for user {user_id}.")
    return count

def has_access(user_id: int, admin_ids: Set[int]) -> bool:
    """
    User has access if they are in admin_ids or have at least one active key in the pool.
    """
    if user_id in admin_ids:
        return True
    with get_db() as conn:
        cur = conn.execute("SELECT 1 FROM api_keys WHERE user_id = ? AND is_active = 1 LIMIT 1", (user_id,))
        return cur.fetchone() is not None

def mask_key(api_key: str) -> str:
    if len(api_key) <= 8:
        return "****"
    return f"{api_key[:4]}...{api_key[-4:]}"

def get_pool_status_for_user(user_id: int) -> Dict[str, Any]:
    """
    Returns pool statistics and masked keys belonging to user_id.
    """
    now = time.time()
    with get_db() as conn:
        # Total active keys
        cur = conn.execute("SELECT COUNT(*) as cnt FROM api_keys WHERE is_active = 1")
        total_active = cur.fetchone()["cnt"]

        # Active keys currently on cooldown
        cur = conn.execute("SELECT COUNT(*) as cnt FROM api_keys WHERE is_active = 1 AND cooldown_until > ?", (now,))
        on_cooldown = cur.fetchone()["cnt"]

        # Available right now
        available = max(0, total_active - on_cooldown)

        # User's keys
        cur = conn.execute(
            "SELECT api_key, is_active, cooldown_until FROM api_keys WHERE user_id = ? ORDER BY id ASC",
            (user_id,)
        )
        user_keys = []
        for r in cur.fetchall():
            masked = mask_key(r["api_key"])
            status_str = "Активен"
            if not r["is_active"]:
                status_str = "Отозван (недействителен)"
            elif r["cooldown_until"] > now:
                secs_left = int(r["cooldown_until"] - now)
                status_str = f"Кулдаун 429 ({secs_left}с)"
            user_keys.append({
                "masked": masked,
                "is_active": bool(r["is_active"]),
                "status": status_str
            })

        return {
            "total_active": total_active,
            "on_cooldown": on_cooldown,
            "available": available,
            "user_keys": user_keys
        }
