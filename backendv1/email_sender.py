"""
Email delivery for worksheets.

Robustness measures over the original version:
  - Supports both implicit-SSL (port 465) and STARTTLS (port 587/25) SMTP
    providers, auto-detected from the port unless SMTP_USE_TLS is set
    explicitly.
  - Retries transient SMTP errors with backoff.
  - Sends a multipart message (plain text + HTML).
  - Attaches both the student worksheet PDF and the parent answer-key PDF
    as separate files so parents can print them selectively.
  - Adds List-Unsubscribe / List-Unsubscribe-Post headers so Gmail and
    Outlook show a native unsubscribe button, reducing spam complaints.
  - Includes an unsubscribe link in the email body.

Bug fix vs. original scheduler.py call:
  The scheduler passes `worksheet_pdf_bytes` and `answer_pdf_bytes` as
  two separate keyword arguments. The previous version of this module
  only accepted a single `pdf_bytes` positional argument, causing a
  TypeError on every delivery. Both parameters are now accepted and
  attached as separate PDF files.
"""
import logging
import smtplib
import time
from email.message import EmailMessage
from email.utils import formataddr

import config

logger = logging.getLogger(__name__)


def _build_message(to_email, child_name, worksheet_pdf_bytes, answer_pdf_bytes,
                   subject, topic, unsubscribe_url=None):
    msg = EmailMessage()
    msg["From"] = formataddr((config.SENDER_NAME, config.SENDER_EMAIL))
    msg["To"] = to_email
    msg["Subject"] = f"{child_name}'s {subject} Worksheet - {topic}"

    if unsubscribe_url:
        # RFC 8058 one-click unsubscribe — Gmail/Outlook show a native button
        msg["List-Unsubscribe"] = f"<{unsubscribe_url}>"
        msg["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"

    plain_body = (
        f"Hello!\n\n"
        f"Here is today's {subject.lower()} worksheet on '{topic}' for {child_name}. "
        f"Just print it out and have fun together.\n\n"
        f"Two PDFs are attached:\n"
        f"  1. {subject}_{topic}_worksheet.pdf  — for your child\n"
        f"  2. {subject}_{topic}_answers.pdf    — answer key (parents only)\n\n"
        f"Happy learning!\n"
    )
    html_body = (
        f"<p>Hello!</p>"
        f"<p>Here is today's <strong>{subject.lower()}</strong> worksheet on "
        f"<strong>{topic}</strong> for <strong>{child_name}</strong>. "
        f"Just print it out and have fun together.</p>"
        f"<p>Two PDFs are attached — the worksheet for your child, and the "
        f"answer key for you.</p>"
        f"<p>Happy learning!</p>"
    )
    if unsubscribe_url:
        plain_body += f"\n--\nNo longer want these? Unsubscribe any time: {unsubscribe_url}\n"
        html_body += (
            f'<p style="color:#888;font-size:12px;">No longer want these? '
            f'<a href="{unsubscribe_url}">Unsubscribe</a> any time.</p>'
        )

    msg.set_content(plain_body)
    msg.add_alternative(html_body, subtype="html")

    # Worksheet PDF (for the child)
    msg.add_attachment(
        bytes(worksheet_pdf_bytes),
        maintype="application",
        subtype="pdf",
        filename=f"{subject}_{topic}_worksheet.pdf",
    )
    # Answer key PDF (for the parent)
    msg.add_attachment(
        bytes(answer_pdf_bytes),
        maintype="application",
        subtype="pdf",
        filename=f"{subject}_{topic}_answers.pdf",
    )
    return msg


def _send_via_smtp(msg):
    if config.SMTP_USE_TLS:
        with smtplib.SMTP(config.SMTP_SERVER, config.SMTP_PORT, timeout=config.SMTP_TIMEOUT) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            smtp.login(config.SENDER_EMAIL, config.SENDER_PASSWORD)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP_SSL(config.SMTP_SERVER, config.SMTP_PORT, timeout=config.SMTP_TIMEOUT) as smtp:
            smtp.login(config.SENDER_EMAIL, config.SENDER_PASSWORD)
            smtp.send_message(msg)


def send_worksheet_email(to_email, child_name, worksheet_pdf_bytes, answer_pdf_bytes,
                         subject, topic, unsubscribe_url=None):
    """Sends the worksheet email (with both PDFs attached), retrying transient failures.

    Raises the last exception if every retry attempt fails, so the caller
    can record the failure and decide whether to back off / pause.
    """
    msg = _build_message(to_email, child_name, worksheet_pdf_bytes, answer_pdf_bytes,
                         subject, topic, unsubscribe_url)
    last_error = None

    for attempt in range(1, config.EMAIL_MAX_RETRIES + 1):
        try:
            _send_via_smtp(msg)
            return
        except smtplib.SMTPAuthenticationError:
            raise RuntimeError(
                "SMTP authentication failed. Check SENDER_EMAIL / SENDER_PASSWORD "
                "(most providers require an app-specific password, not your normal login password)."
            ) from None
        except smtplib.SMTPRecipientsRefused:
            raise
        except (smtplib.SMTPException, OSError, TimeoutError) as e:
            last_error = e
            logger.warning("Email send attempt %s/%s to %s failed: %s",
                           attempt, config.EMAIL_MAX_RETRIES, to_email, e)
            if attempt < config.EMAIL_MAX_RETRIES:
                time.sleep(min(2 ** attempt, 10))

    raise RuntimeError(
        f"Failed to send email to {to_email} after {config.EMAIL_MAX_RETRIES} attempts: {last_error}"
    )


def send_admin_alert(subject_line, body):
    """Best-effort notification to the admin (e.g. repeated delivery
    failures). Never raises -- a broken alert channel shouldn't take
    down the scheduler."""
    if not config.ADMIN_EMAIL:
        return
    try:
        msg = EmailMessage()
        msg["From"] = formataddr((config.SENDER_NAME, config.SENDER_EMAIL))
        msg["To"] = config.ADMIN_EMAIL
        msg["Subject"] = f"[Worksheet Automation] {subject_line}"
        msg.set_content(body)
        _send_via_smtp(msg)
    except Exception as e:  # noqa: BLE001
        logger.error("Failed to send admin alert email: %s", e)