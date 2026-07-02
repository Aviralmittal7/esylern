"""
Central configuration for Worksheet Automation.

All settings are read from environment variables (loaded from .env via
python-dotenv). Nothing here makes network calls, so importing this module
is always safe and fast.

validate() performs the "do we have what we need to run" check and is
called explicitly at startup (wsgi.py / agent.py), never at import time.
"""
import os
import logging
import dotenv

dotenv.load_dotenv()


def _get_bool(name, default=False):
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def _get_int(name, default):
    val = os.getenv(name)
    if val is None or val.strip() == "":
        return default
    try:
        return int(val)
    except ValueError:
        logging.warning("Env var %s=%r is not a valid integer; using default %s", name, val, default)
        return default


def _get_list(name, default=None):
    val = os.getenv(name)
    if not val:
        return default or []
    return [item.strip() for item in val.split(",") if item.strip()]


# ---------------------------------------------------------------------------
# LLM (Groq)
# ---------------------------------------------------------------------------
GROQ_API_KEY         = os.getenv("GROQ_API_KEY")
GROQ_MODEL           = os.getenv("GROQ_MODEL", "llama-3.3-70b-versatile")
GROQ_MODEL_FALLBACKS = _get_list(
    "GROQ_MODEL_FALLBACKS",
    default=["llama3-70b-8192", "llama-3.1-8b-instant"],
)
GROQ_TEMPERATURE     = float(os.getenv("GROQ_TEMPERATURE", "0.7"))
GROQ_MAX_TOKENS      = _get_int("GROQ_MAX_TOKENS", 1500)
GROQ_REQUEST_TIMEOUT = _get_int("GROQ_REQUEST_TIMEOUT", 30)
GROQ_MAX_RETRIES     = _get_int("GROQ_MAX_RETRIES", 3)

# ---------------------------------------------------------------------------
# Email (SMTP)
# ---------------------------------------------------------------------------
SMTP_SERVER     = os.getenv("SMTP_SERVER", "smtp.gmail.com")
SMTP_PORT       = _get_int("SMTP_PORT", 465)
_smtp_use_tls   = os.getenv("SMTP_USE_TLS")
SMTP_USE_TLS    = _get_bool("SMTP_USE_TLS") if _smtp_use_tls is not None else (SMTP_PORT != 465)
SENDER_EMAIL    = os.getenv("SENDER_EMAIL")
SENDER_NAME     = os.getenv("SENDER_NAME", "Worksheet Buddy")
SENDER_PASSWORD = os.getenv("SENDER_PASSWORD")
SMTP_TIMEOUT    = _get_int("SMTP_TIMEOUT", 30)
EMAIL_MAX_RETRIES = _get_int("EMAIL_MAX_RETRIES", 3)
ADMIN_EMAIL     = os.getenv("ADMIN_EMAIL")   # alerted on repeated failures

# ---------------------------------------------------------------------------
# Web / API
# ---------------------------------------------------------------------------
FLASK_SECRET_KEY = os.getenv("FLASK_SECRET_KEY", "dev-secret-key-change-me")
FLASK_HOST       = os.getenv("FLASK_HOST", "0.0.0.0")
# Render/Railway/Heroku-style platforms inject PORT automatically and expect
# the app to bind to it. Prefer that over FLASK_PORT so the app always
# matches whatever port the platform actually assigned (avoids the
# "New primary port detected, restarting" churn in Render logs).
FLASK_PORT       = _get_int("PORT", _get_int("FLASK_PORT", 8008))
FLASK_DEBUG      = _get_bool("FLASK_DEBUG", False)
APP_BASE_URL     = os.getenv("APP_BASE_URL", f"http://localhost:{_get_int('FLASK_PORT', 8008)}")

# Comma-separated list of origins allowed to call /api/*.
# e.g. "https://yoursite.netlify.app,http://localhost:5500"
ALLOWED_ORIGINS  = _get_list("ALLOWED_ORIGINS", default=["http://localhost:5500"])

# ---------------------------------------------------------------------------
# Stripe (optional — payments / trial-to-paid conversion)
# ---------------------------------------------------------------------------
STRIPE_SECRET_KEY     = os.getenv("STRIPE_SECRET_KEY")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET")
# Payment links from your Stripe Dashboard → Products → Payment Links
STRIPE_BASIC_LINK   = os.getenv("STRIPE_BASIC_LINK",   "https://buy.stripe.com/YOUR_BASIC_LINK")
STRIPE_PREMIUM_LINK = os.getenv("STRIPE_PREMIUM_LINK", "https://buy.stripe.com/YOUR_PREMIUM_LINK")

# ---------------------------------------------------------------------------
# Scheduling / business rules
# ---------------------------------------------------------------------------
TIMEZONE               = os.getenv("TIMEZONE", "Asia/Kolkata")
TRIAL_DAYS             = _get_int("TRIAL_DAYS", 7)
MAX_CONSECUTIVE_FAILURES = _get_int("MAX_CONSECUTIVE_FAILURES", 3)
TRIAL_SWEEP_HOUR       = _get_int("TRIAL_SWEEP_HOUR", 1)  # 0-23

# ---------------------------------------------------------------------------
# Misc
# ---------------------------------------------------------------------------
LOG_LEVEL        = os.getenv("LOG_LEVEL", "INFO").upper()
DB_PATH_OVERRIDE = os.getenv("DB_PATH")   # mainly for tests
CSV_PATH_OVERRIDE = os.getenv("CSV_PATH") # mainly for tests


def configure_logging():
    logging.basicConfig(
        level=getattr(logging, LOG_LEVEL, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    logging.getLogger("fontTools").setLevel(logging.WARNING)


def validate(require_email=True):
    """
    Raise RuntimeError with a clear message if required vars are missing.

    Call this once at startup. Never call it at import time.
    """
    problems = []
    if APP_BASE_URL.startswith("http://localhost"):
        logging.warning(
            "APP_BASE_URL is '%s' — unsubscribe links in emails will be broken in production. "
            "Set APP_BASE_URL=https://your-backend.onrender.com in your environment.",
            APP_BASE_URL,
        )
    if not GROQ_API_KEY:
        problems.append("GROQ_API_KEY is not set (needed to generate worksheets).")
    if require_email:
        if not SENDER_EMAIL:
            problems.append("SENDER_EMAIL is not set.")
        if not SENDER_PASSWORD:
            problems.append("SENDER_PASSWORD is not set. Use an app password, not your login password.")
    if problems:
        details = "\n  - ".join(problems)
        raise RuntimeError(
            "Missing required configuration:\n  - "
            + details
            + "\n\nCopy .env.example to .env and fill in the missing values."
        )
