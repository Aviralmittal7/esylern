"""
Persistence layer for Worksheet Automation.

Everything that touches SQLite lives here, behind small repository-style
helper functions, so webapp.py and scheduler.py never write raw SQL.
This makes it much easier to keep the two callers (the Flask app and the
APScheduler background thread) consistent and crash-resistant.
"""
import os
import secrets
import sqlite3
from contextlib import contextmanager
from datetime import date, timedelta

import config

DB_DIR = os.path.join(os.path.dirname(__file__), "data")
DB_PATH = config.DB_PATH_OVERRIDE or os.path.join(DB_DIR, "worksheets.db")

VALID_STATUSES = ("trial", "active", "expired", "paused", "paused_error", "cancelled")
VALID_DIFFICULTIES = ("easier", "normal", "challenge", "review")

SCHEMA = """
    PRAGMA foreign_keys = ON;

    CREATE TABLE IF NOT EXISTS parents (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT NOT NULL,
        email TEXT UNIQUE NOT NULL COLLATE NOCASE,
        child_name TEXT NOT NULL,
        grade_level TEXT NOT NULL,
        subject_focus TEXT NOT NULL,
        preferred_schedule TEXT NOT NULL,
        plan TEXT NOT NULL DEFAULT 'free',
        status TEXT NOT NULL DEFAULT 'trial',
        unsubscribe_token TEXT UNIQUE,
        consecutive_failures INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        last_sent_at TIMESTAMP,
        trial_start DATE DEFAULT (date('now')),
        trial_end DATE DEFAULT (date('now', '+7 days')),
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS preferences (
        parent_id INTEGER PRIMARY KEY,
        topic TEXT,
        difficulty_mode TEXT DEFAULT 'normal',
        feedback TEXT,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(parent_id) REFERENCES parents(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS delivery_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        parent_id INTEGER NOT NULL,
        worksheet_file TEXT,
        topic TEXT,
        difficulty TEXT,
        status TEXT NOT NULL DEFAULT 'sent',
        error_message TEXT,
        summary TEXT,
        sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(parent_id) REFERENCES parents(id) ON DELETE CASCADE
    );

    CREATE INDEX IF NOT EXISTS idx_parents_status ON parents(status);
    CREATE INDEX IF NOT EXISTS idx_delivery_log_parent_id ON delivery_log(parent_id);
"""


def get_db():
    """Open a new SQLite connection with sane, concurrency-friendly defaults.

    A Flask request thread and the APScheduler background thread can both
    touch the database at the same time. WAL mode + a busy timeout lets
    SQLite queue short waits instead of immediately raising
    'database is locked'.
    """
    os.makedirs(DB_DIR, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA busy_timeout = 10000")
    return conn


@contextmanager
def db_session():
    """Context manager that always closes the connection and commits on
    success / rolls back on error, so callers can't leak connections."""
    conn = get_db()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    os.makedirs(DB_DIR, exist_ok=True)
    with db_session() as conn:
        conn.executescript(SCHEMA)


# ---------------------------------------------------------------------------
# Parents
# ---------------------------------------------------------------------------

def create_parent(name, email, child_name, grade_level, subject_focus,
                  preferred_schedule, plan="free", trial_days=None):
    """Insert a new parent + default preferences row.

    Returns the new parent_id, or None if the email is already registered.
    """
    trial_days = config.TRIAL_DAYS if trial_days is None else trial_days
    trial_end = date.today() + timedelta(days=trial_days)
    token = secrets.token_urlsafe(24)

    with db_session() as conn:
        existing = conn.execute(
            "SELECT id FROM parents WHERE email = ?", (email,)
        ).fetchone()
        if existing:
            return None

        cursor = conn.execute(
            """
            INSERT INTO parents
                (name, email, child_name, grade_level, subject_focus,
                 preferred_schedule, plan, status, unsubscribe_token, trial_start, trial_end)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'trial', ?, date('now'), ?)
            """,
            (name, email, child_name, grade_level, subject_focus,
             preferred_schedule, plan, token, trial_end.isoformat()),
        )
        parent_id = cursor.lastrowid
        conn.execute(
            "INSERT INTO preferences (parent_id, topic, difficulty_mode) VALUES (?, ?, 'normal')",
            (parent_id, subject_focus),
        )
        return parent_id


def get_parent(parent_id):
    with db_session() as conn:
        return conn.execute("SELECT * FROM parents WHERE id = ?", (parent_id,)).fetchone()


def get_parent_by_email(email):
    with db_session() as conn:
        return conn.execute("SELECT * FROM parents WHERE email = ?", (email,)).fetchone()


def get_parent_by_token(token):
    with db_session() as conn:
        return conn.execute(
            "SELECT * FROM parents WHERE unsubscribe_token = ?", (token,)
        ).fetchone()


def list_parents(status=None):
    with db_session() as conn:
        if status:
            return conn.execute(
                "SELECT * FROM parents WHERE status = ?", (status,)
            ).fetchall()
        return conn.execute("SELECT * FROM parents").fetchall()


def set_parent_status(parent_id, status):
    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid status '{status}', must be one of {VALID_STATUSES}")
    with db_session() as conn:
        conn.execute(
            "UPDATE parents SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (status, parent_id),
        )


def get_preferences(parent_id):
    with db_session() as conn:
        return conn.execute(
            "SELECT * FROM preferences WHERE parent_id = ?",(parent_id,)
        ).fetchone()


def record_delivery_success(parent_id, worksheet_file, topic, difficulty, summary):
    with db_session() as conn:
        conn.execute(
            """
            INSERT INTO delivery_log (parent_id, worksheet_file, topic, difficulty, status, summary)
            VALUES (?, ?, ?, ?, 'sent', ?)
            """,
            (parent_id, worksheet_file, topic, difficulty, summary),
        )
        conn.execute(
            """
            UPDATE parents
            SET consecutive_failures = 0, last_error = NULL,
                last_sent_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (parent_id,),
        )


def record_delivery_failure(parent_id, topic, difficulty, error_message):
    """Logs the failure and returns the parent's updated consecutive_failures count."""
    with db_session() as conn:
        conn.execute(
            """
            INSERT INTO delivery_log (parent_id, topic, difficulty, status, error_message)
            VALUES (?, ?, ?, 'failed', ?)
            """,
            (parent_id, topic, difficulty, str(error_message)[:2000]),
        )
        conn.execute(
            """
            UPDATE parents
            SET consecutive_failures = consecutive_failures + 1,
                last_error = ?, updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (str(error_message)[:2000], parent_id),
        )
        row = conn.execute(
            "SELECT consecutive_failures FROM parents WHERE id = ?", (parent_id,)
        ).fetchone()
        return row["consecutive_failures"] if row else None


def recent_topics(parent_id, limit=5):
    """Topics most recently sent to this parent, newest first -- used to
    nudge the worksheet generator toward variety instead of repeats."""
    with db_session() as conn:
        rows = conn.execute(
            """
            SELECT topic FROM delivery_log
            WHERE parent_id = ? AND status = 'sent' AND topic IS NOT NULL AND topic != ''
            ORDER BY sent_at DESC, id DESC LIMIT ?
            """,
            (parent_id, limit),
        ).fetchall()
        # de-duplicate while preserving order
        seen, out = set(), []
        for r in rows:
            if r["topic"] not in seen:
                seen.add(r["topic"])
                out.append(r["topic"])
        return out


def find_expired_trials():
    with db_session() as conn:
        return conn.execute(
            "SELECT * FROM parents WHERE status = 'trial' AND trial_end < date('now')"
        ).fetchall()


if __name__ == "__main__":
    init_db()
    print(f"Database initialised at {DB_PATH}")