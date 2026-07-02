"""
CSV backup/export for parent signups.

Every successful signup (API or HTML form) is appended here in addition to
SQLite, so you can open the file in Excel/Sheets without querying the DB.
"""
import csv
import os
import threading
from datetime import datetime, timezone

import config

DATA_DIR = os.path.join(os.path.dirname(__file__), "data")
CSV_PATH = config.CSV_PATH_OVERRIDE or os.path.join(DATA_DIR, "signups.csv")

CSV_HEADERS = [
    "parent_id",
    "parent_name",
    "email",
    "child_name",
    "grade_level",
    "subject_focus",
    "preferred_schedule",
    "plan",
    "source",
    "created_at",
]

_lock = threading.Lock()


def init_csv():
    """Create the CSV file with a header row if it does not exist yet."""
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.exists(CSV_PATH):
        return
    with _lock:
        if os.path.exists(CSV_PATH):
            return
        with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(CSV_HEADERS)


def append_signup(
    parent_id,
    name,
    email,
    child_name,
    grade_level,
    subject_focus,
    preferred_schedule,
    plan="free",
    source="api",
):
    """Append one signup row. Returns the CSV path on success."""
    init_csv()
    row = {
        "parent_id": parent_id,
        "parent_name": name,
        "email": email,
        "child_name": child_name,
        "grade_level": grade_level,
        "subject_focus": subject_focus,
        "preferred_schedule": preferred_schedule,
        "plan": plan,
        "source": source,
        "created_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }
    with _lock:
        with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
            csv.DictWriter(f, fieldnames=CSV_HEADERS).writerow(row)
    return CSV_PATH


if __name__ == "__main__":
    init_csv()
    print(f"CSV store ready at {CSV_PATH}")
