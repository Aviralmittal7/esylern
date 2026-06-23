# Worksheet Automation — API Backend

Generates AI-powered worksheets (via Groq) and emails them to parents
on a per-parent schedule. All signup and configuration data comes via
a JSON API — there is no server-rendered web form.

---

## How it works

1. Your frontend (Netlify, Vercel, plain HTML/JS) calls `POST /api/signup`.
2. A 7-day free trial starts and the first worksheet is emailed immediately.
3. A background scheduler generates fresh worksheets (via Groq), renders
   them as PDFs, and emails them on the parent's chosen schedule.
4. When the trial ends, delivery stops and the parent gets an upgrade notice.
5. Optional: Stripe webhook automates trial → paid conversion.

---

## Quick start (local)

```bash
python3 -m venv venv
source venv/bin/activate       # Windows: venv\Scripts\activate
pip install -r requirements.txt

cp .env.example .env
# Edit .env — at minimum set GROQ_API_KEY, SENDER_EMAIL, SENDER_PASSWORD

python agent.py
```

The backend is now running at `http://localhost:8008`.

> Use `python agent.py`, not `python webapp.py`. `agent.py` starts both
> the scheduler and the API server; running `webapp.py` alone only
> serves the API with no scheduled delivery.

---

## Deploying to Render (free tier)

1. Push this folder to a GitHub repository.
2. Go to [render.com](https://render.com) → **New → Web Service**.
3. Connect the repo. Render auto-detects the `Procfile`.
4. Set the environment variables (copy from `.env.example`).
5. Deploy. Your public URL is shown in the Render dashboard.
6. Set `APP_BASE_URL` to that URL and redeploy.

> **One worker only.** The `Procfile` sets `--workers 1` intentionally.
> APScheduler runs inside the process; multiple workers would each start
> their own scheduler and send duplicate worksheets.

### Render environment variables

Set these in the Render dashboard under **Environment**:

| Variable | Notes |
|---|---|
| `GROQ_API_KEY` | From console.groq.com |
| `SENDER_EMAIL` | Your Gmail address |
| `SENDER_PASSWORD` | Gmail App Password (not your login password) |
| `FLASK_SECRET_KEY` | Any long random string |
| `APP_BASE_URL` | Your Render URL, e.g. `https://myapp.onrender.com` |
| `ALLOWED_ORIGINS` | Your frontend URL, e.g. `https://mysite.netlify.app` |

---

## API reference

### `GET /health`
Uptime check. Returns `200` when the server is running.

```json
{ "status": "ok" }
```

---

### `GET /api/form-options`
Returns all valid values for dropdown fields. Call this once on page load.

```json
{
  "grades":   ["Grade 1", "Grade 2", ..., "Grade 8"],
  "subjects": ["Maths", "English", "Science", "Geography", "History", "Art"],
  "plans": [
    { "value": "free",    "label": "Free 7-Day Trial (1 subject)" },
    { "value": "basic",   "label": "Basic – 3 subjects/month" },
    { "value": "premium", "label": "Premium – Unlimited subjects" }
  ],
  "frequencies": [
    { "value": "daily",         "label": "Daily (Mon–Fri)" },
    { "value": "weekly-sunday", "label": "Weekly on Sunday" }
  ]
}
```

---

### `POST /api/signup`

Register a new parent. Rate-limited to **5 requests per IP per hour**.

**Request body (JSON):**

```json
{
  "parentName":   "Priya Sharma",
  "email":        "priya@example.com",
  "childName":    "Rohan",
  "grade":        "Grade 4",
  "subjects":     ["Maths", "Science"],
  "frequency":    "daily",
  "deliveryTime": "05:00 PM",
  "plan":         "free"
}
```

| Field | Type | Required | Notes |
|---|---|---|---|
| `parentName` | string | ✅ | |
| `email` | string | ✅ | Must be a valid email |
| `childName` | string | ✅ | |
| `grade` | string | ✅ | Must be one of `/api/form-options` grades |
| `subjects` | string[] | ✅ | At least one; must be from form-options |
| `frequency` | string | ✅ | `"daily"` or `"weekly-sunday"` |
| `deliveryTime` | string | ✅ | Format: `"05:00 PM"` (12-hour clock) |
| `plan` | string | — | Defaults to `"free"` |

**Success (201):**
```json
{ "success": true, "childName": "Rohan", "email": "priya@example.com" }
```

**Validation error (400):**
```json
{ "success": false, "error": "'email' is required." }
```

**Duplicate email (409):**
```json
{ "success": false, "error": "This email is already registered." }
```

**Example fetch call:**
```javascript
const res = await fetch("https://your-backend.onrender.com/api/signup", {
  method: "POST",
  headers: { "Content-Type": "application/json" },
  body: JSON.stringify({
    parentName:   "Priya Sharma",
    email:        "priya@example.com",
    childName:    "Rohan",
    grade:        "Grade 4",
    subjects:     ["Maths"],
    frequency:    "daily",
    deliveryTime: "05:00 PM",
    plan:         "free",
  }),
});
const data = await res.json();
if (data.success) {
  // Show "check your inbox" message
} else {
  // Show data.error to the user
}
```

---

### `GET /unsubscribe/<token>`

One-click unsubscribe linked from every worksheet email. Returns a
minimal HTML page (this URL is opened in a browser from an email client).

---

### `POST /webhook/stripe`

Stripe webhook for automated trial → paid conversion. See the Stripe
section below.

---

## Stripe integration (optional)

If you want paid plans, add this to your Render environment:

```
STRIPE_SECRET_KEY=sk_live_...
STRIPE_WEBHOOK_SECRET=whsec_...
STRIPE_BASIC_LINK=https://buy.stripe.com/...
STRIPE_PREMIUM_LINK=https://buy.stripe.com/...
```

Then in the Stripe Dashboard → Webhooks, add:
- **URL:** `https://your-backend.onrender.com/webhook/stripe`
- **Event:** `checkout.session.completed`

On your Payment Links, add metadata `plan: basic` or `plan: premium`.

When a payment comes in, the parent's status flips to `active`
automatically and worksheet delivery resumes.

---

## Project structure

```
agent.py               Entrypoint: starts scheduler + API server together
webapp.py              Flask API routes (/health, /api/*, /unsubscribe, /webhook/stripe)
wsgi.py                Production WSGI entrypoint (used by gunicorn/Procfile)
scheduler.py           APScheduler: per-parent delivery jobs + daily trial sweep
worksheet_generator.py Groq LLM call → worksheet text + answer key
pdf_creator.py         Renders Unicode-safe PDFs (student + answer key)
email_sender.py        SMTP sending (SSL or STARTTLS) with retries
db.py                  SQLite schema + all database helpers
csv_store.py           CSV export of every signup (open in Excel/Sheets)
config.py              All env-var configuration in one place
Procfile               Render/Railway deployment command
.env.example           Template for your .env file
fonts/                 Bundled DejaVu Sans TTFs (full Unicode coverage)
tests/                 Offline unit tests (no Groq/SMTP calls needed)
```

## Running tests

```bash
pip install pytest
python -m pytest tests/ -v
```

All tests mock the network boundary, so they work without any credentials.
