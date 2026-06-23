"""
Production WSGI entrypoint.

Gunicorn imports this module after forking the worker process (no
--preload). The scheduler starts fresh inside the worker — avoiding the
fork-safety issue where APScheduler's background thread and internal
locks don't survive a fork intact, causing jobs to queue but never fire.

Run locally:
    gunicorn wsgi:app --bind 0.0.0.0:8008 --workers 1

On Render / Railway the Procfile does this automatically.

IMPORTANT: keep --workers 1. APScheduler's BackgroundScheduler runs
inside the process; multiple workers would each spawn their own scheduler
and deliver each worksheet multiple times.
"""
import config
import csv_store
import db
from scheduler import start_scheduler
from webapp import app  # noqa: F401  (re-exported for gunicorn)

config.configure_logging()
config.validate()
db.init_db()
csv_store.init_csv()
start_scheduler()
