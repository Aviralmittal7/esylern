"""
Background scheduling for worksheet delivery.

Robustness measures over the original version:
  - Logs through the standard `logging` module instead of bare `print`.
  - On repeated delivery failure for a parent (LLM down, bad email, SMTP
    misconfigured, etc.) the job is auto-paused after
    config.MAX_CONSECUTIVE_FAILURES failures, instead of silently retrying
    forever on the same schedule and burning API/SMTP quota. An optional
    admin alert email is sent when this happens.
  - A daily sweep job finds parents whose trial has expired, flips their
    status, removes their cron job, and sends a single polite "trial
    ended" email with subscription links -- instead of the original
    behaviour of checking trial_end inline on every single scheduled run
    forever after expiry.
  - Per-job `misfire_grace_time` and `coalesce` so a brief downtime
    doesn't cause a pile-up of missed runs firing back-to-back on restart.
  - Uses the configured TIMEZONE for cron evaluation instead of the
    server's local timezone.
  - Recent topics are pulled from delivery history and passed to the
    generator to nudge variety across consecutive worksheets.
"""
import logging
import threading
from datetime import date

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import config
import db
from worksheet_generator import generate_worksheet_text
from pdf_creator import create_worksheet_pdf, create_answer_pdf
from email_sender import send_worksheet_email, send_admin_alert

logger = logging.getLogger(__name__)

_scheduler = None
_lock = threading.Lock()

TRIAL_SWEEP_JOB_ID = "trial_expiry_sweep"


def _pick_subject(parent_id, subjects):
    """Rotate through a parent's chosen subjects one delivery at a time.

    Uses the total count of successful deliveries modulo the number of
    subjects so each worksheet cycles to the next subject in the list.
    For a single-subject parent this always returns that one subject.
    """
    if not subjects:
        return "Maths"
    if len(subjects) == 1:
        return subjects[0]
    count = db.count_successful_deliveries(parent_id)
    return subjects[count % len(subjects)]


def _unsubscribe_url(parent_row):
    if not parent_row["unsubscribe_token"]:
        return None
    return f"{config.APP_BASE_URL.rstrip('/')}/unsubscribe/{parent_row['unsubscribe_token']}"


def process_parent(parent_id):
    logger.info("process_parent: starting for parent_id=%s", parent_id)
    topic = difficulty = None
    try:
        parent = db.get_parent(parent_id)
        if not parent:
            logger.warning("process_parent called for unknown parent_id=%s", parent_id)
            return

        if parent["status"] not in ("trial", "active"):
            logger.info("Skipping %s (%s): status is '%s', not trial/active.",
                        parent["child_name"], parent["email"], parent["status"])
            return

        prefs = db.get_preferences(parent_id)
        difficulty = (prefs["difficulty_mode"] if prefs and prefs["difficulty_mode"] else "normal")

        subjects = [s.strip() for s in parent["subject_focus"].split(",") if s.strip()]
        current_subject = _pick_subject(parent_id, subjects)

        pref_topic = prefs["topic"] if prefs and prefs["topic"] else ""
        if pref_topic and pref_topic != parent["subject_focus"] and "," not in pref_topic:
            topic = pref_topic
        else:
            topic = current_subject

        avoid_topics = db.recent_topics(parent_id, limit=5)

        logger.info("Generating %s worksheet for %s (%s, subject=%s, topic=%s)",
                    difficulty, parent["child_name"], parent["email"], current_subject, topic)

        student_text, answer_key, model_used = generate_worksheet_text(
            grade=parent["grade_level"], subject=current_subject, topic=topic,
            difficulty=difficulty, avoid_topics=avoid_topics,
        )
        worksheet_bytes = create_worksheet_pdf(student_text, parent["child_name"], current_subject, topic)
        answer_bytes = create_answer_pdf(answer_key, parent["child_name"], current_subject, topic)

        send_worksheet_email(
            to_email=parent["email"], child_name=parent["child_name"],
            worksheet_pdf_bytes=worksheet_bytes, answer_pdf_bytes=answer_bytes,
            subject=current_subject, topic=topic,
            unsubscribe_url=_unsubscribe_url(parent),
        )

        db.record_delivery_success(
            parent_id, worksheet_file=f"{current_subject}_{topic}_worksheet.pdf",
            topic=topic, difficulty=difficulty, summary=f"Sent via model {model_used}",
        )
        logger.info("-> Sent to %s", parent["email"])

    except Exception as e:  # noqa: BLE001
        logger.exception("Failed to process worksheet for parent_id=%s: %s", parent_id, e)
        try:
            failures = db.record_delivery_failure(parent_id, topic or "", difficulty or "normal", e)
        except Exception:
            failures = None
        if failures is not None and failures >= config.MAX_CONSECUTIVE_FAILURES:
            logger.error("Pausing parent_id=%s after %s consecutive failures.", parent_id, failures)
            db.set_parent_status(parent_id, "paused_error")
            remove_parent_job(parent_id)
            send_admin_alert(
                subject_line=f"Paused delivery for parent_id={parent_id}",
                body=f"Worksheet delivery for parent_id={parent_id} paused after {failures} consecutive failures.\n\nLast error: {e}",
            )


def _run_trial_sweep():
    """Daily housekeeping: expire trials past their end date, stop their
    jobs, and send a single courteous notice with subscription links."""
    expired = db.find_expired_trials()
    for parent in expired:
        logger.info("Trial expired for %s (%s); deactivating.", parent["child_name"], parent["email"])
        db.set_parent_status(parent["id"], "expired")
        remove_parent_job(parent["id"])
        try:
            from email.message import EmailMessage

            # ---------------------------------------------------------------------------
            # TODO: Replace the placeholder Stripe Payment Link URLs below with your
            # real links from the Stripe dashboard (Products → Payment Links).
            # Create two recurring products: Basic and Premium, then paste the links.
            # ---------------------------------------------------------------------------
            basic_link = config.STRIPE_BASIC_LINK or "https://buy.stripe.com/YOUR_BASIC_LINK"
            premium_link = config.STRIPE_PREMIUM_LINK or "https://buy.stripe.com/YOUR_PREMIUM_LINK"

            msg = EmailMessage()
            msg["From"] = config.SENDER_EMAIL
            msg["To"] = parent["email"]
            msg["Subject"] = f"{parent['child_name']}'s free trial has ended"
            msg.set_content(
                f"Hi {parent['name']},\n\n"
                f"The {config.TRIAL_DAYS}-day free trial of daily worksheets for "
                f"{parent['child_name']} has ended, so we've paused delivery.\n\n"
                f"To keep the worksheets coming, choose a plan below:\n\n"
                f"  Basic (3 subjects)   -> {basic_link}\n"
                f"  Premium (unlimited)  -> {premium_link}\n\n"
                f"Worksheets resume automatically the moment your payment is confirmed.\n\n"
                f"Thanks for trying esylern!"
            )
            msg.add_alternative(
                f"<p>Hi {parent['name']},</p>"
                f"<p>The {config.TRIAL_DAYS}-day free trial of daily worksheets for "
                f"<strong>{parent['child_name']}</strong> has ended, so we've paused delivery.</p>"
                f"<p>To keep the worksheets coming, choose a plan:</p>"
                f"<ul>"
                f"<li><a href='{basic_link}'>Basic – 3 subjects/month</a></li>"
                f"<li><a href='{premium_link}'>Premium – Unlimited subjects</a></li>"
                f"</ul>"
                f"<p>Worksheets resume automatically the moment your payment is confirmed.</p>"
                f"<p>Thanks for trying esylern!</p>",
                subtype="html",
            )
            from email_sender import _send_via_smtp
            _send_via_smtp(msg)
        except Exception as e:  # noqa: BLE001
            logger.warning("Could not send trial-ended notice to %s: %s", parent["email"], e)


def add_parent_job(parent_id):
    """Add or replace a parent's recurring worksheet job on the live scheduler."""
    global _scheduler
    with _lock:
        if not _scheduler:
            logger.warning("add_parent_job called but scheduler is not running (parent_id=%s).", parent_id)
            return False
        parent = db.get_parent(parent_id)
        if not parent:
            return False
        try:
            trigger = CronTrigger.from_crontab(parent["preferred_schedule"], timezone=config.TIMEZONE)
        except Exception as e:
            logger.error("Invalid cron schedule for parent_id=%s ('%s'): %s",
                         parent_id, parent["preferred_schedule"], e)
            return False

        _scheduler.add_job(
            process_parent,
            trigger=trigger,
            args=[parent_id],
            id=f"parent_{parent_id}",
            replace_existing=True,
            coalesce=True,
            misfire_grace_time=3600,
            max_instances=1,
        )
        logger.info("Scheduled job for %s with cron '%s' (tz=%s).",
                    parent["child_name"], parent["preferred_schedule"], config.TIMEZONE)
        return True


def remove_parent_job(parent_id):
    global _scheduler
    with _lock:
        if not _scheduler:
            return
        job_id = f"parent_{parent_id}"
        if _scheduler.get_job(job_id):
            _scheduler.remove_job(job_id)
            logger.info("Removed scheduled job for parent_id=%s.", parent_id)


def send_now(parent_id):
    """Run a parent's worksheet job immediately, off the request thread,
    using the scheduler's own executor. Falls back to running inline if
    the scheduler isn't up yet (e.g. webapp.py run standalone for local
    testing without agent.py)."""
    global _scheduler
    with _lock:
        scheduler = _scheduler
    if scheduler:
        scheduler.add_job(process_parent, args=[parent_id],
                          id=f"welcome_{parent_id}_{date.today()}",
                          misfire_grace_time=3600, replace_existing=True)
    else:
        logger.warning("Scheduler not running; sending welcome worksheet inline for parent_id=%s.", parent_id)
        threading.Thread(target=process_parent, args=(parent_id,), daemon=True).start()


def start_scheduler():
    global _scheduler
    with _lock:
        if _scheduler:
            return _scheduler
        _scheduler = BackgroundScheduler(timezone=config.TIMEZONE)

    for parent in db.list_parents():
        if parent["status"] in ("trial", "active"):
            add_parent_job(parent["id"])

    with _lock:
        _scheduler.add_job(
            _run_trial_sweep,
            trigger=CronTrigger(hour=config.TRIAL_SWEEP_HOUR, minute=0, timezone=config.TIMEZONE),
            id=TRIAL_SWEEP_JOB_ID,
            replace_existing=True,
            coalesce=True,
            misfire_grace_time=3600,
        )
        _scheduler.start()
    logger.info("Scheduler started (timezone=%s).", config.TIMEZONE)
    return _scheduler


def shutdown_scheduler():
    global _scheduler
    with _lock:
        if _scheduler:
            _scheduler.shutdown(wait=False)
            _scheduler = None
