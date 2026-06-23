"""
Persistence layer for Worksheet Automation – PostgreSQL on Render.

Opens a fresh connection per db_session to avoid stale/broken SSL connections.
Uses CITEXT for case‑insensitive email lookups and RealDictCursor for row access.
"""

import os
import secrets
from contextlib import contextmanager
from datetime import date, timedelta

import psycopg2
import psycopg2.extras

import config

# ---------------------------------------------------------------------------
# Database URL – provided by Render
# ---------------------------------------------------------------------------
DATABASE_URL = os.getenv("DATABASE_URL")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL environment variable must be set")

# Ensure SSL and keep-alive settings (Render requires SSL)
_EXTRA_PARAMS = "?sslmode=require&connect_timeout=10&keepalives=1&keepalives_idle=30&keepalives_interval=10&keepalives_count=5"
if "?" in DATABASE_URL:
    # Append if not already present
    if "sslmode" not in DATABASE_URL:
        DATABASE_URL += "&sslmode=require&connect_timeout=10&keepalives=1&keepalives_idle=30&keepalives_interval=10&keepalives_count=5"
else:
    DATABASE_URL += _EXTRA_PARAMS

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
VALID_STATUSES = ("trial", "active", "expired", "paused", "paused_error", "cancelled")
VALID_DIFFICULTIES = ("easier", "normal", "challenge", "review")

# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
SCHEMA = """
    CREATE EXTENSION IF NOT EXISTS citext;

    CREATE TABLE IF NOT EXISTS parents (
        id SERIAL PRIMARY KEY,
        name TEXT NOT NULL,
        email CITEXT UNIQUE NOT NULL,
        child_name TEXT NOT NULL,
        grade_level TEXT NOT NULL,
        subject_focus TEXT NOT NULL,
        preferred_schedule TEXT NOT NULL,
        plan TEXT NOT NULL DEFAULT 'free',
        status TEXT NOT NULL DEFAULT 'trial',
        unsubscribe_token TEXT UNIQUE,
        consecutive_failures INTEGER NOT NULL DEFAULT 0,
        last_error TEXT,
        last_sent_at TIMESTAMPTZ,
        trial_start DATE DEFAULT CURRENT_DATE,
        trial_end DATE DEFAULT (CURRENT_DATE + INTERVAL '7 days'),
        created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
    );

    CREATE TABLE IF NOT EXISTS preferences (
        parent_id INTEGER PRIMARY KEY,
        topic TEXT,
        difficulty_mode TEXT DEFAULT 'normal',
        feedback TEXT,
        updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(parent_id) REFERENCES parents(id) ON DELETE CASCADE
    );

    CREATE TABLE IF NOT EXISTS delivery_log (
        id SERIAL PRIMARY KEY,
        parent_id INTEGER NOT NULL,
        worksheet_file TEXT,
        topic TEXT,
        difficulty TEXT,
        status TEXT NOT NULL DEFAULT 'sent',
        error_message TEXT,
        summary TEXT,
        sent_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY(parent_id) REFERENCES parents(id) ON DELETE CASCADE
    );

    CREATE INDEX IF NOT EXISTS idx_parents_status ON parents(status);
    CREATE INDEX IF NOT EXISTS idx_delivery_log_parent_id ON delivery_log(parent_id);
"""

# ---------------------------------------------------------------------------
# Connection helper (opens a new connection every time)
# ---------------------------------------------------------------------------
def _get_connection():
    """Create a fresh psycopg2 connection with SSL and keepalive settings."""
    conn = psycopg2.connect(
        DATABASE_URL,
        cursor_factory=psycopg2.extras.RealDictCursor,
    )
    conn.autocommit = False
    return conn


@contextmanager
def db_session():
    """Context manager that opens a new connection, commits on success,
    rolls back on error, and always closes the connection."""
    conn = _get_connection()
    try:
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    """Run schema creation (idempotent). Call once at app startup."""
    conn = _get_connection()
    try:
        with conn.cursor() as cur:
            cur.execute(SCHEMA)
        conn.commit()
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Parents – function signatures completely unchanged
# ---------------------------------------------------------------------------

def create_parent(name, email, child_name, grade_level, subject_focus,
                  preferred_schedule, plan="free", trial_days=None):
    trial_days = config.TRIAL_DAYS if trial_days is None else trial_days
    trial_end = date.today() + timedelta(days=trial_days)
    token = secrets.token_urlsafe(24)

    with db_session() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM parents WHERE email = %s", (email,))
            if cur.fetchone():
                return None

            cur.execute(
                """
                INSERT INTO parents
                    (name, email, child_name, grade_level, subject_focus,
                     preferred_schedule, plan, status, unsubscribe_token, trial_start, trial_end)
                VALUES (%s, %s, %s, %s, %s, %s, %s, 'trial', %s, CURRENT_DATE, %s)
                RETURNING id
                """,
                (name, email, child_name, grade_level, subject_focus,
                 preferred_schedule, plan, token, trial_end.isoformat()),
            )
            parent_id = cur.fetchone()["id"]

            cur.execute(
                "INSERT INTO preferences (parent_id, topic, difficulty_mode) VALUES (%s, %s, 'normal')",
                (parent_id, subject_focus),
            )
            return parent_id


def get_parent(parent_id):
    with db_session() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM parents WHERE id = %s", (parent_id,))
            return cur.fetchone()


def get_parent_by_email(email):
    with db_session() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM parents WHERE email = %s", (email,))
            return cur.fetchone()


def get_parent_by_token(token):
    with db_session() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM parents WHERE unsubscribe_token = %s", (token,))
            return cur.fetchone()


def list_parents(status=None):
    with db_session() as conn:
        with conn.cursor() as cur:
            if status:
                cur.execute("SELECT * FROM parents WHERE status = %s", (status,))
            else:
                cur.execute("SELECT * FROM parents")
            return cur.fetchall()


def set_parent_status(parent_id, status):
    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid status '{status}', must be one of {VALID_STATUSES}")
    with db_session() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE parents SET status = %s, updated_at = CURRENT_TIMESTAMP WHERE id = %s",
                (status, parent_id),
            )


def get_preferences(parent_id):
    with db_session() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM preferences WHERE parent_id = %s", (parent_id,))
            return cur.fetchone()


def record_delivery_success(parent_id, worksheet_file, topic, difficulty, summary):
    with db_session() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO delivery_log (parent_id, worksheet_file, topic, difficulty, status, summary)
                VALUES (%s, %s, %s, %s, 'sent', %s)
                """,
                (parent_id, worksheet_file, topic, difficulty, summary),
            )
            cur.execute(
                """
                UPDATE parents
                SET consecutive_failures = 0, last_error = NULL,
                    last_sent_at = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                (parent_id,),
            )


def record_delivery_failure(parent_id, topic, difficulty, error_message):
    with db_session() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO delivery_log (parent_id, topic, difficulty, status, error_message)
                VALUES (%s, %s, %s, 'failed', %s)
                """,
                (parent_id, topic, difficulty, str(error_message)[:2000]),
            )
            cur.execute(
                """
                UPDATE parents
                SET consecutive_failures = consecutive_failures + 1,
                    last_error = %s, updated_at = CURRENT_TIMESTAMP
                WHERE id = %s
                """,
                (str(error_message)[:2000], parent_id),
            )
            cur.execute(
                "SELECT consecutive_failures FROM parents WHERE id = %s", (parent_id,)
            )
            row = cur.fetchone()
            return row["consecutive_failures"] if row else None


def recent_topics(parent_id, limit=5):
    with db_session() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT topic FROM delivery_log
                WHERE parent_id = %s AND status = 'sent' AND topic IS NOT NULL AND topic != ''
                ORDER BY sent_at DESC, id DESC LIMIT %s
                """,
                (parent_id, limit),
            )
            rows = cur.fetchall()
            seen, out = set(), []
            for r in rows:
                if r["topic"] not in seen:
                    seen.add(r["topic"])
                    out.append(r["topic"])
            return out


def count_successful_deliveries(parent_id):
    with db_session() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS cnt FROM delivery_log WHERE parent_id = %s AND status = 'sent'",
                (parent_id,),
            )
            row = cur.fetchone()
            return row["cnt"] if row else 0


def find_expired_trials():
    with db_session() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM parents WHERE status = 'trial' AND trial_end < CURRENT_DATE"
            )
            return cur.fetchall()


# ---------------------------------------------------------------------------
if __name__ == "__main__":
    init_db()
    print("PostgreSQL database initialised.")
