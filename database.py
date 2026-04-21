"""
database.py — SQLite persistence layer.

Tables:
  accounts  — watched usernames + status tracking
  admins    — authorised Telegram user IDs
  settings  — key/value config store
"""

import sqlite3
import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

DB_PATH = "monitor.db"
_lock = threading.Lock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def init_db():
    """Create all tables if they don't exist."""
    with _lock:
        conn = get_conn()
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS accounts (
                username            TEXT PRIMARY KEY,
                added_by            INTEGER NOT NULL,
                added_at            TEXT    NOT NULL,
                last_checked        TEXT,
                last_status         TEXT    DEFAULT 'unknown',
                prev_status         TEXT,
                status_changed_at   TEXT,
                scheduler_saw_banned INTEGER DEFAULT 0,
                ban_alerted         INTEGER DEFAULT 0,
                unban_alerted       INTEGER DEFAULT 0
            );

            CREATE TABLE IF NOT EXISTS admins (
                user_id   INTEGER PRIMARY KEY,
                added_at  TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS settings (
                key   TEXT PRIMARY KEY,
                value TEXT
            );
        """)
        conn.commit()
        conn.close()
    logger.info("Database initialised at %s", DB_PATH)


# ── Account CRUD ──────────────────────────────────────────────────────────────

def add_account(username: str, added_by: int) -> bool:
    """Add account to watchlist. Returns True if newly added."""
    username = username.lower().lstrip("@")
    with _lock:
        conn = get_conn()
        try:
            conn.execute(
                """INSERT INTO accounts
                   (username, added_by, added_at, status_changed_at)
                   VALUES (?, ?, ?, ?)""",
                (username, added_by, _now(), _now())
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False
        finally:
            conn.close()


def remove_account(username: str) -> bool:
    """Remove account. Returns True if it existed."""
    username = username.lower().lstrip("@")
    with _lock:
        conn = get_conn()
        cur = conn.execute("DELETE FROM accounts WHERE username = ?", (username,))
        conn.commit()
        conn.close()
        return cur.rowcount > 0


def get_all_accounts() -> list[dict]:
    with _lock:
        conn = get_conn()
        rows = conn.execute("SELECT * FROM accounts").fetchall()
        conn.close()
        return [dict(r) for r in rows]


def get_account(username: str) -> Optional[dict]:
    username = username.lower().lstrip("@")
    with _lock:
        conn = get_conn()
        row = conn.execute(
            "SELECT * FROM accounts WHERE username = ?", (username,)
        ).fetchone()
        conn.close()
        return dict(row) if row else None


def update_status(username: str, new_status: str):
    """
    Update account status. Sets scheduler_saw_banned=1 when status is 'banned'.
    Resets alert flags when status changes.
    """
    username = username.lower()
    with _lock:
        conn = get_conn()
        row = conn.execute(
            "SELECT last_status, scheduler_saw_banned FROM accounts WHERE username = ?",
            (username,)
        ).fetchone()
        if not row:
            conn.close()
            return

        old_status = row["last_status"]
        saw_banned = row["scheduler_saw_banned"]

        # Set scheduler_saw_banned when we confirm a ban
        if new_status == "banned":
            saw_banned = 1

        # When status changes, reset the alerted flag for new state
        ban_alerted_reset = ""
        unban_alerted_reset = ""
        status_changed_at_update = ""

        if old_status != new_status:
            status_changed_at_update = ", status_changed_at = ?"
            if new_status == "banned":
                ban_alerted_reset = ", ban_alerted = 0"
            elif new_status == "active":
                unban_alerted_reset = ", unban_alerted = 0"

        sql = f"""UPDATE accounts SET
                    last_status = ?,
                    prev_status = ?,
                    last_checked = ?,
                    scheduler_saw_banned = ?
                    {ban_alerted_reset}
                    {unban_alerted_reset}
                    {status_changed_at_update}
                  WHERE username = ?"""

        params = [new_status, old_status, _now(), saw_banned]
        if status_changed_at_update:
            params.append(_now())
        params.append(username)

        conn.execute(sql, params)
        conn.commit()
        conn.close()


def needs_ban_alert(username: str) -> bool:
    with _lock:
        conn = get_conn()
        row = conn.execute(
            "SELECT last_status, ban_alerted FROM accounts WHERE username = ?",
            (username,)
        ).fetchone()
        conn.close()
        if not row: return False
        return row["last_status"] == "banned" and row["ban_alerted"] == 0


def needs_unban_alert(username: str) -> bool:
    """Only fires if scheduler itself previously saw the account as banned."""
    with _lock:
        conn = get_conn()
        row = conn.execute(
            """SELECT last_status, scheduler_saw_banned, unban_alerted
               FROM accounts WHERE username = ?""",
            (username,)
        ).fetchone()
        conn.close()
        if not row: return False
        return (
            row["last_status"] == "active"
            and row["scheduler_saw_banned"] == 1   # ← must have been confirmed banned
            and row["unban_alerted"] == 0
        )


def mark_ban_alerted(username: str):
    with _lock:
        conn = get_conn()
        conn.execute("UPDATE accounts SET ban_alerted = 1 WHERE username = ?", (username,))
        conn.commit()
        conn.close()


def mark_unban_alerted(username: str):
    with _lock:
        conn = get_conn()
        conn.execute("UPDATE accounts SET unban_alerted = 1 WHERE username = ?", (username,))
        conn.commit()
        conn.close()


def get_status_changed_at(username: str) -> Optional[str]:
    with _lock:
        conn = get_conn()
        row = conn.execute(
            "SELECT status_changed_at FROM accounts WHERE username = ?", (username,)
        ).fetchone()
        conn.close()
        return row["status_changed_at"] if row else None


# ── Admin management ──────────────────────────────────────────────────────────

def add_admin(user_id: int) -> bool:
    with _lock:
        conn = get_conn()
        try:
            conn.execute(
                "INSERT INTO admins (user_id, added_at) VALUES (?, ?)",
                (user_id, _now())
            )
            conn.commit()
            return True
        except sqlite3.IntegrityError:
            return False
        finally:
            conn.close()


def remove_admin(user_id: int) -> bool:
    with _lock:
        conn = get_conn()
        cur = conn.execute("DELETE FROM admins WHERE user_id = ?", (user_id,))
        conn.commit()
        conn.close()
        return cur.rowcount > 0


def get_admins() -> list[int]:
    with _lock:
        conn = get_conn()
        rows = conn.execute("SELECT user_id FROM admins").fetchall()
        conn.close()
        return [r["user_id"] for r in rows]


def is_admin(user_id: int, owner_ids: list[int]) -> bool:
    return user_id in owner_ids or user_id in get_admins()
