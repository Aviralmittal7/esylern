"""
API-only Flask backend for Worksheet Automation.

All data arrives and leaves as JSON. The only exception is
/unsubscribe/<token>, which is linked from emails and must return a
minimal browser-friendly page.

Routes
------
GET  /health                 — uptime check
GET  /api/form-options       — grades / subjects / frequencies / plans
POST /api/signup             — register a new parent (rate-limited)
GET  /unsubscribe/<token>    — one-click unsubscribe (email link)
POST /webhook/stripe         — Stripe checkout.session.completed

CORS
----
Only /api/* is opened. Set ALLOWED_ORIGINS in .env to match your
frontend URL(s). No trailing slashes.
"""
import logging
import re
import os
from flask import Flask, request, jsonify
from flask_cors import CORS
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

import config
import db
import csv_store
from scheduler import add_parent_job, send_now

logger = logging.getLogger(__name__)
DEBUG_KEY = os.getenv("DEBUG_KEY")  # shared secret for /api/debug/run/<id>; unset = route disabled
app = Flask(__name__)
app.secret_key = config.FLASK_SECRET_KEY

# Rate limiter — memory-backed (fine for single-worker; swap to
# storage_uri="redis://..." for multi-process deployments).
limiter = Limiter(
    get_remote_address,
    app=app,
    default_limits=[],
    storage_uri="memory://",
)

CORS(app, resources={r"/api/*": {"origins": config.ALLOWED_ORIGINS}})

EMAIL_RE = re.compile(r"^[^\@\s]+@[^\@\s]+\.[^\@\s]+$")

GRADES = [f"Grade {i}" for i in range(1, 9)]
SUBJECTS = ["Maths", "English", "Science", "Geography", "History", "Art"]
PLANS = [
    {"value": "free",    "label": "Free 7-Day Trial (1 subject)"},
    {"value": "basic",   "label": "Basic – 3 subjects/month"},
    {"value": "premium", "label": "Premium – Unlimited subjects"},
]
FREQUENCIES = {
    "daily":         "1-5",   # Mon-Fri
    "weekly-sunday": "0",
}

from datetime import datetime

def _parse_delivery_time(time_str):
    parsed = datetime.strptime(time_str.strip().upper(), "%I:%M %p")
    return parsed.hour, parsed.minute


def _build_cron(frequency, time_str):
    dow = FREQUENCIES.get(frequency)
    if dow is None:
        raise ValueError(f"Unknown frequency '{frequency}'")
    hour, minute = _parse_delivery_time(time_str)
    return f"{minute} {hour} * * {dow}"


def _validate_signup(data):
    for field in ("parentName", "email", "childName", "grade"):
        if not str(data.get(field, "")).strip():
            return f"'{field}' is required."
    if not EMAIL_RE.match(str(data.get("email", "")).strip()):
        return "Invalid email address."
    if data.get("grade") not in GRADES:
        return f"Invalid grade. Must be one of: {', '.join(GRADES)}."

    subjects = data.get("subjects")
    if not isinstance(subjects, list) or not subjects:
        return "At least one subject is required."
    invalid = [s for s in subjects if s not in SUBJECTS]
    if invalid:
        return f"Unknown subject(s): {', '.join(invalid)}."

    if data.get("frequency") not in FREQUENCIES:
        return f"Invalid frequency. Accepted values: {', '.join(FREQUENCIES)}."

    try:
        _parse_delivery_time(str(data.get("deliveryTime", "")))
    except ValueError:
        return "Invalid deliveryTime. Expected format: '05:00 PM'."

    plan = data.get("plan")
    if plan and plan not in {p["value"] for p in PLANS}:
        return f"Invalid plan. Accepted values: {', '.join(p['value'] for p in PLANS)}."

    return None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/")
def index():
    return jsonify({
        "service": "esylern worksheet API",
        "status": "ok",
        "docs": "GET /api/form-options, POST /api/signup",
    })


@app.route("/health")
def health():
    return jsonify({"status": "ok"})


@app.route("/api/form-options", methods=["GET"])
def api_form_options():
    """Return all valid option values so the frontend never has to hardcode them."""
    return jsonify({
        "grades":   GRADES,
        "subjects": SUBJECTS,
        "plans":    PLANS,
        "frequencies": [
            {"value": "daily",         "label": "Daily (Mon–Fri)"},
            {"value": "weekly-sunday", "label": "Weekly on Sunday"},
        ],
    })


@app.route("/api/signup", methods=["POST"])
@limiter.limit("5 per hour")
def api_signup():
    """
    Register a new parent and kick off their first worksheet.

    Request body (JSON)
    -------------------
    {
      "parentName":   "Priya Sharma",
      "email":        "priya@example.com",
      "childName":    "Rohan",
      "grade":        "Grade 4",
      "subjects":     ["Maths", "Science"],
      "frequency":    "daily",
      "deliveryTime": "05:00 PM",
      "plan":         "free"           // optional, defaults to "free"
    }

    Success response (201)
    ----------------------
    { "success": true, "childName": "Rohan", "email": "priya@example.com" }

    Error responses
    ---------------
    400  — validation error   { "success": false, "error": "<message>" }
    409  — email duplicate    { "success": false, "error": "Already registered." }
    """
    data = request.get_json(silent=True) or {}

    error = _validate_signup(data)
    if error:
        return jsonify({"success": False, "error": error}), 400

    try:
        cron = _build_cron(data["frequency"], data["deliveryTime"])
    except ValueError as e:
        return jsonify({"success": False, "error": str(e)}), 400

    parent_id = db.create_parent(
        name=str(data["parentName"]).strip(),
        email=str(data["email"]).strip().lower(),
        child_name=str(data["childName"]).strip(),
        grade_level=data["grade"],
        subject_focus=",".join(data["subjects"]),
        preferred_schedule=cron,
        plan=data.get("plan") or "free",
    )

    if parent_id is None:
        return jsonify({
            "success": False,
            "error": "This email is already registered.",
        }), 409

    csv_store.append_signup(
        parent_id=parent_id,
        name=str(data["parentName"]).strip(),
        email=str(data["email"]).strip().lower(),
        child_name=str(data["childName"]).strip(),
        grade_level=data["grade"],
        subject_focus=",".join(data["subjects"]),
        preferred_schedule=cron,
        plan=data.get("plan") or "free",
        source="api",
    )

    add_parent_job(parent_id)
    send_now(parent_id)

    logger.info(
        "New signup: parent_id=%s email=%s plan=%s",
        parent_id, data["email"], data.get("plan", "free"),
    )

    return jsonify({
        "success":   True,
        "childName": str(data["childName"]).strip(),
        "email":     str(data["email"]).strip(),
    }), 201


@app.route("/unsubscribe/<token>")
def unsubscribe(token):
    """
    One-click unsubscribe linked from every worksheet email.
    Returns a minimal HTML page (this route is opened in a browser).
    """
    parent = db.get_parent_by_token(token)

    if not parent or parent["status"] == "cancelled":
        return (
            "<html><body style='font-family:sans-serif;max-width:480px;margin:60px auto'>"
            "<h2>Link not recognised</h2>"
            "<p>This subscription may already be cancelled.</p>"
            "</body></html>"
        ), 404

    db.set_parent_status(parent["id"], "cancelled")
    from scheduler import remove_parent_job
    remove_parent_job(parent["id"])
    logger.info("Unsubscribed parent_id=%s email=%s", parent["id"], parent["email"])

    return (
        "<html><body style='font-family:sans-serif;max-width:480px;margin:60px auto'>"
        f"<h2>You're unsubscribed</h2>"
        f"<p>{parent['child_name']} will no longer receive worksheets.</p>"
        "<p>You're always welcome to sign up again.</p>"
        "</body></html>"
    )

@app.route("/api/debug/run/<int:parent_id>", methods=["POST"])
def debug_run_parent(parent_id):
    """Run process_parent synchronously, bypassing the scheduler, and
    return the resulting delivery_log row. Lets you see the *real* error
    in the HTTP response instead of digging through Render logs."""
    if not config.DEBUG_KEY or request.headers.get("X-Debug-Key") != config.DEBUG_KEY:
        return jsonify({"error": "unauthorized"}), 401

    from scheduler import process_parent
    process_parent(parent_id)

    with db.db_session() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, error_message, summary, sent_at FROM delivery_log "
                "WHERE parent_id = %s ORDER BY id DESC LIMIT 1",
                (parent_id,),
            )
            row = cur.fetchone()

    return jsonify(dict(row) if row else {"note": "no delivery_log row written yet"}), 200


@app.route("/api/debug/run/<int:parent_id>", methods=["POST"])
def debug_run_parent(parent_id):
    """Run process_parent synchronously, bypassing the scheduler, and
    return the resulting delivery_log row. Lets you see the *real* error
    in the HTTP response instead of digging through Render logs."""
    if not config.DEBUG_KEY or request.headers.get("X-Debug-Key") != config.DEBUG_KEY:
        return jsonify({"error": "unauthorized"}), 401

    from scheduler import process_parent
    process_parent(parent_id)

    with db.db_session() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT status, error_message, summary, sent_at FROM delivery_log "
                "WHERE parent_id = %s ORDER BY id DESC LIMIT 1",
                (parent_id,),
            )
            row = cur.fetchone()

    return jsonify(dict(row) if row else {"note": "no delivery_log row written yet"}), 200

@app.route("/webhook/stripe", methods=["POST"])
def stripe_webhook():
    """
    Listens for Stripe checkout.session.completed and activates the
    parent's subscription.

    Setup in Stripe Dashboard → Webhooks:
      URL:    https://your-backend.onrender.com/webhook/stripe
      Events: checkout.session.completed

    On your Payment Links, add metadata:
      plan: basic   (or premium)
    """
    if not config.STRIPE_SECRET_KEY:
        logger.warning("Stripe webhook called but STRIPE_SECRET_KEY is not set.")
        return "", 400

    try:
        import stripe  # noqa: PLC0415
        stripe.api_key = config.STRIPE_SECRET_KEY
    except ImportError:
        logger.error("stripe package not installed. Run: pip install stripe")
        return "", 500

    payload    = request.get_data()
    sig_header = request.headers.get("Stripe-Signature")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, config.STRIPE_WEBHOOK_SECRET
        )
    except (ValueError, stripe.error.SignatureVerificationError) as e:
        logger.warning("Invalid Stripe webhook: %s", e)
        return "", 400

    if event["type"] == "checkout.session.completed":
        session = event["data"]["object"]
        email   = (session.get("customer_details") or {}).get("email") or session.get("customer_email")
        plan    = (session.get("metadata") or {}).get("plan", "basic")

        if email:
            parent = db.get_parent_by_email(email.lower())
            if parent:
                db.set_parent_status(parent["id"], "active")
                add_parent_job(parent["id"])
                logger.info(
                    "Stripe payment confirmed: activated parent_id=%s email=%s plan=%s",
                    parent["id"], email, plan,
                )
            else:
                logger.warning("Stripe webhook: no parent found for email=%s", email)

    return "", 200


@app.errorhandler(404)
def not_found(_):
    return jsonify({"error": "Not found"}), 404


@app.errorhandler(405)
def method_not_allowed(_):
    return jsonify({"error": "Method not allowed"}), 405


if __name__ == "__main__":
    config.configure_logging()
    db.init_db()
    csv_store.init_csv()
    logger.warning(
        "Running webapp.py standalone — scheduled delivery is NOT active. "
        "Use `python agent.py` or gunicorn via wsgi.py for full operation."
    )
    app.run(host=config.FLASK_HOST, port=config.FLASK_PORT, debug=config.FLASK_DEBUG, threaded=True)
