"""
Email delivery for worksheets – SMTP + optional HTTPS API (Resend).

Uses Resend's API when RESEND_API_KEY is set; otherwise falls back to SMTP.
This avoids Render's SMTP port block on the free tier.
"""

import logging
import os
import smtplib
import time
from email.message import EmailMessage
from email.utils import formataddr

import requests

import config

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Decide which transport to use
# ---------------------------------------------------------------------------
_RESEND_API_KEY = os.getenv("RESEND_API_KEY", "").strip()
_USE_API = bool(_RESEND_API_KEY)

if _USE_API:
    logger.info("Email sending: using Resend API")
else:
    logger.info("Email sending: using SMTP (ports 465/587/25)")


def _build_message(to_email, child_name, worksheet_pdf_bytes, answer_pdf_bytes,
                   subject, topic, unsubscribe_url=None):
    """
    Build the EmailMessage object (used by SMTP path).
    The API path builds its own payload – this is kept for SMTP only.
    """
    msg = EmailMessage()
    msg["From"] = formataddr((config.SENDER_NAME, config.SENDER_EMAIL))
    msg["To"] = to_email
    msg["Subject"] = f"{child_name}'s {subject} Worksheet - {topic}"

    if unsubscribe_url:
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

    # Attach both PDFs
    msg.add_attachment(
        bytes(worksheet_pdf_bytes),
        maintype="application",
        subtype="pdf",
        filename=f"{subject}_{topic}_worksheet.pdf",
    )
    msg.add_attachment(
        bytes(answer_pdf_bytes),
        maintype="application",
        subtype="pdf",
        filename=f"{subject}_{topic}_answers.pdf",
    )
    return msg


# ---------------------------------------------------------------------------
# SMTP sending (original, unchanged)
# ---------------------------------------------------------------------------
def _send_via_smtp(msg):
    """Deliver a pre‑built EmailMessage via SMTP."""
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


# ---------------------------------------------------------------------------
# HTTPS API sending (Resend)
# ---------------------------------------------------------------------------
def _send_via_api(to_email, child_name, worksheet_pdf_bytes, answer_pdf_bytes,
                  subject, topic, unsubscribe_url=None):
    """
    Send worksheet email using Resend's API (HTTPS).
    Attachments are encoded as base64 in the JSON payload.
    """
    import base64

    # Build HTML body (same as SMTP, but we don't use EmailMessage)
    html_body = (
        f"<p>Hello!</p>"
        f"<p>Here is today's <strong>{subject.lower()}</strong> worksheet on "
        f"<strong>{topic}</strong> for <strong>{child_name}</strong>. "
        f"Just print it out and have fun together.</p>"
        f"<p>Two PDFs are attached — the worksheet for your child, and the "
        f"answer key for you.</p>"
        f"<p>Happy learning!</p>"
    )
    plain_body = (
        f"Hello!\n\n"
        f"Here is today's {subject.lower()} worksheet on '{topic}' for {child_name}. "
        f"Just print it out and have fun together.\n\n"
        f"Two PDFs are attached:\n"
        f"  1. {subject}_{topic}_worksheet.pdf  — for your child\n"
        f"  2. {subject}_{topic}_answers.pdf    — answer key (parents only)\n\n"
        f"Happy learning!\n"
    )
    if unsubscribe_url:
        plain_body += f"\n--\nNo longer want these? Unsubscribe any time: {unsubscribe_url}\n"
        html_body += (
            f'<p style="color:#888;font-size:12px;">No longer want these? '
            f'<a href="{unsubscribe_url}">Unsubscribe</a> any time.</p>'
        )

    payload = {
        "from": f"{config.SENDER_NAME} <{config.SENDER_EMAIL}>",
        "to": [to_email],
        "subject": f"{child_name}'s {subject} Worksheet - {topic}",
        "html": html_body,
        "text": plain_body,
        "attachments": [
            {
                "filename": f"{subject}_{topic}_worksheet.pdf",
                "content": base64.b64encode(bytes(worksheet_pdf_bytes)).decode("utf-8"),
                "type": "application/pdf",
            },
            {
                "filename": f"{subject}_{topic}_answers.pdf",
                "content": base64.b64encode(bytes(answer_pdf_bytes)).decode("utf-8"),
                "type": "application/pdf",
            },
        ],
    }

    # Add unsubscribe headers if available (Resend supports custom headers)
    if unsubscribe_url:
        payload["headers"] = {
            "List-Unsubscribe": f"<{unsubscribe_url}>",
            "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
        }

    response = requests.post(
        "https://api.resend.com/emails",
        json=payload,
        headers={"Authorization": f"Bearer {_RESEND_API_KEY}"},
        timeout=30,
    )
    if response.status_code not in (200, 201):
        raise RuntimeError(f"Resend API error: {response.status_code} {response.text}")


# ---------------------------------------------------------------------------
# Main entry point (unchanged signature)
# ---------------------------------------------------------------------------
def send_worksheet_email(to_email, child_name, worksheet_pdf_bytes, answer_pdf_bytes,
                         subject, topic, unsubscribe_url=None):
    """Sends the worksheet email, retrying transient failures.

    Automatically chooses SMTP or HTTPS API based on RESEND_API_KEY env var.
    """
    if _USE_API:
        last_error = None
        for attempt in range(1, config.EMAIL_MAX_RETRIES + 1):
            try:
                _send_via_api(to_email, child_name,
                              worksheet_pdf_bytes, answer_pdf_bytes,
                              subject, topic, unsubscribe_url)
                return
            except Exception as e:
                last_error = e
                logger.warning("Email send attempt %s/%s to %s failed: %s",
                               attempt, config.EMAIL_MAX_RETRIES, to_email, e)
                if attempt < config.EMAIL_MAX_RETRIES:
                    time.sleep(min(2 ** attempt, 10))
        raise RuntimeError(
            f"Failed to send email to {to_email} after {config.EMAIL_MAX_RETRIES} attempts: {last_error}"
        )
    else:
        # Original SMTP path
        msg = _build_message(to_email, child_name,
                             worksheet_pdf_bytes, answer_pdf_bytes,
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
    """Best‑effort admin alert. Uses API if available, else SMTP."""
    if not config.ADMIN_EMAIL:
        return
    try:
        if _USE_API:
            payload = {
                "from": f"{config.SENDER_NAME} <{config.SENDER_EMAIL}>",
                "to": [config.ADMIN_EMAIL],
                "subject": f"[Worksheet Automation] {subject_line}",
                "text": body,
            }
            response = requests.post(
                "https://api.resend.com/emails",
                json=payload,
                headers={"Authorization": f"Bearer {_RESEND_API_KEY}"},
                timeout=10,
            )
            if response.status_code not in (200, 201):
                logger.error("Admin alert API error: %s %s", response.status_code, response.text)
        else:
            msg = EmailMessage()
            msg["From"] = formataddr((config.SENDER_NAME, config.SENDER_EMAIL))
            msg["To"] = config.ADMIN_EMAIL
            msg["Subject"] = f"[Worksheet Automation] {subject_line}"
            msg.set_content(body)
            _send_via_smtp(msg)
    except Exception as e:
        logger.error("Failed to send admin alert email: %s", e)
