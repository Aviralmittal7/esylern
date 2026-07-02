"""
Main entrypoint: runs the background scheduler and the Flask signup form
together in one process.

Run with:  python agent.py
"""
import logging
import signal
import sys
import threading

import config
import csv_store
import db
from scheduler import start_scheduler, shutdown_scheduler
from webapp import app

logger = logging.getLogger(__name__)

_shutdown_event = threading.Event()


def run_webapp():
    app.run(host=config.FLASK_HOST, port=config.FLASK_PORT, debug=False,
             use_reloader=False, threaded=True)


def _handle_signal(signum, frame):
    logger.info("Received signal %s, shutting down...", signum)
    _shutdown_event.set()


def main():
    config.configure_logging()
    try:
        config.validate()
    except RuntimeError as e:
        logger.error(str(e))
        sys.exit(1)

    db.init_db()
    csv_store.init_csv()

    scheduler = start_scheduler()

    web_thread = threading.Thread(target=run_webapp, daemon=True)
    web_thread.start()

    active_count = len(db.list_parents(status="trial")) + len(db.list_parents(status="active"))
    logger.info("Web signup form running at %s", config.APP_BASE_URL)
    logger.info("%s active/trial subscriber(s) loaded.", active_count)

    signal.signal(signal.SIGINT, _handle_signal)
    try:
        signal.signal(signal.SIGTERM, _handle_signal)
    except (ValueError, AttributeError):
        pass  # SIGTERM not available on this platform (e.g. some Windows setups)

    try:
        _shutdown_event.wait()
    finally:
        shutdown_scheduler()
        logger.info("Shutdown complete.")


if __name__ == "__main__":
    main()
