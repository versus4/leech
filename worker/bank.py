"""
Account bank: a sqlite pool of pre-harvested accounts (token + saved session +
the proxy/IP the account was born on).

The harvester fills it in the background; run_prompt claims from it instantly.
This is what pulls signup OUT of the hot path.
"""
import os
import sqlite3
import threading
import time

from . import config

_lock = threading.Lock()


def _open() -> sqlite3.Connection:
    os.makedirs(os.path.dirname(config.BANK_PATH) or ".", exist_ok=True)
    c = sqlite3.connect(config.BANK_PATH)
    c.execute("""
        CREATE TABLE IF NOT EXISTS accounts(
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            email      TEXT,
            token      TEXT,
            state_path TEXT,
            proxy      TEXT,                       -- json of the proxy used at signup
            status     TEXT DEFAULT 'fresh',       -- fresh | used | dead
            created    REAL,
            used_at    REAL
        )""")
    try:
        c.execute("ALTER TABLE accounts ADD COLUMN proxy TEXT")
    except sqlite3.OperationalError:
        pass
    return c


def add(email: str, token: str, state_path: str, proxy: str | None = None) -> None:
    with _lock:
        c = _open()
        try:
            c.execute(
                "INSERT INTO accounts(email, token, state_path, proxy, created) "
                "VALUES(?,?,?,?,?)",
                (email, token, state_path, proxy, time.time()),
            )
            c.commit()
        finally:
            c.close()


def claim() -> dict | None:
    """Atomically grab the oldest fresh account and mark it used."""
    with _lock:
        c = _open()
        try:
            row = c.execute(
                "SELECT id, email, token, state_path, proxy FROM accounts "
                "WHERE status='fresh' ORDER BY created LIMIT 1"
            ).fetchone()
            if not row:
                return None
            c.execute("UPDATE accounts SET status='used', used_at=? WHERE id=?",
                      (time.time(), row[0]))
            c.commit()
            return {"id": row[0], "email": row[1], "token": row[2],
                    "state_path": row[3], "proxy": row[4]}
        finally:
            c.close()


def mark_dead(acct_id: int) -> None:
    with _lock:
        c = _open()
        try:
            c.execute("UPDATE accounts SET status='dead' WHERE id=?", (acct_id,))
            c.commit()
        finally:
            c.close()


def count_fresh() -> int:
    with _lock:
        c = _open()
        try:
            return c.execute("SELECT COUNT(*) FROM accounts WHERE status='fresh'").fetchone()[0]
        finally:
            c.close()


def stats() -> dict:
    with _lock:
        c = _open()
        try:
            rows = c.execute("SELECT status, COUNT(*) FROM accounts GROUP BY status").fetchall()
            return {s: n for s, n in rows}
        finally:
            c.close()
