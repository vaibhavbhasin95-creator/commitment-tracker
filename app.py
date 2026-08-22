"""
Meeting Commitment Tracker — local prototype backend.

Takes the place of a real transcript-parsing connector for now: meetings and
their extracted commitments are either demo-seeded or entered by hand, and
this app tracks status and (mock) nudges for them.

No real authentication — a visitor just types an email address, which is
upserted into `users` and stored in a signed session cookie. Every other
route reads that email out of the session.

Nudges are UI-only: "send test nudge" writes a row to `nudge_log` and
returns it. Nothing is ever sent over the network — `nudge_settings` exists
so the shape (per-user webhook URL / API key, channel, cadence) is realistic
for when a real integration gets wired in later.

Same mock approach for the external task tracker (Jira / Google Sheets):
`tracker_settings` holds one provider config per user, and "sync to tracker"
writes a row to `tracker_sync_log` describing what *would* be sent — no real
Jira/Sheets API call is made. Wiring up the real API calls (OAuth for both
providers, in practice) is future work once a provider is chosen; users who
don't configure a tracker are unaffected — the built-in status tracking
above works exactly as before.

Run:
    pip install flask
    python app.py
Then open http://localhost:5000 in a browser.
"""

import json
import sqlite3
from datetime import date, datetime, timedelta
from pathlib import Path

from flask import Flask, jsonify, request, send_from_directory, session

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "commitment_tracker.db"
SCHEMA_PATH = BASE_DIR / "schema.sql"

app = Flask(__name__, static_folder="static", static_url_path="")
app.secret_key = "dev-only-secret-change-before-any-real-deployment"


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(SCHEMA_PATH.read_text())


def now_iso():
    return datetime.utcnow().isoformat(timespec="seconds")


def today_str():
    return date.today().isoformat()


def current_email():
    return session.get("email")


def require_email():
    """Returns the session email, or None if the caller isn't identified."""
    email = current_email()
    if not email:
        return None
    return email


# ---------- static frontend ----------

@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


@app.route("/dashboard")
def dashboard_page():
    return send_from_directory(app.static_folder, "dashboard.html")


@app.route("/settings")
def settings_page():
    return send_from_directory(app.static_folder, "settings.html")


# ---------- identify (no real auth) ----------

@app.route("/api/identify", methods=["POST"])
def identify():
    data = request.get_json(force=True)
    email = (data.get("email") or "").strip().lower()
    if not email or "@" not in email:
        return jsonify({"error": "a valid email is required"}), 400

    db = get_db()
    existing = db.execute("SELECT id FROM users WHERE email = ?", (email,)).fetchone()
    if not existing:
        db.execute(
            "INSERT INTO users (email, created_at) VALUES (?, ?)", (email, now_iso())
        )
        db.commit()
    db.close()

    session["email"] = email
    return jsonify({"email": email})


@app.route("/api/me", methods=["GET"])
def me():
    email = current_email()
    if not email:
        return jsonify({"error": "not identified"}), 401
    return jsonify({"email": email})


@app.route("/api/logout", methods=["POST"])
def logout():
    session.pop("email", None)
    return jsonify({"ok": True})


# ---------- meetings ----------

@app.route("/api/meetings", methods=["GET"])
def list_meetings():
    email = require_email()
    if not email:
        return jsonify({"error": "not identified"}), 401
    db = get_db()
    rows = db.execute(
        "SELECT * FROM meetings WHERE owner_email = ? ORDER BY meeting_date DESC",
        (email,),
    ).fetchall()
    db.close()
    return jsonify([dict(r) for r in rows])


DEMO_MEETINGS = [
    {
        "title": "Product Sync — Q3 Roadmap",
        "days_ago": 3,
        "source": "generated",
        "transcript_excerpt": (
            "...so let's lock the Q3 priorities. I'll get the updated pricing "
            "deck over to sales by Thursday. And I'll take a look at that "
            "onboarding email bug and have a fix out before end of week. "
            "Let's also find time next week to review the new dashboard "
            "mocks together..."
        ),
        "commitments": [
            {
                "task_text": "Send updated pricing deck to the sales team",
                "owner_name": "Priya Shah",
                "owner_email": None,
                "due_in_days": 2,
                "confidence_score": 0.94,
                "source_quote": "I'll get the updated pricing deck over to sales by Thursday.",
                "assign_to_self": False,
            },
            {
                "task_text": "Fix the onboarding email bug",
                "owner_name": "You",
                "due_in_days": -1,
                "confidence_score": 0.88,
                "source_quote": "I'll take a look at that onboarding email bug and have a fix out before end of week.",
                "assign_to_self": True,
            },
            {
                "task_text": "Schedule follow-up with design on the new dashboard mocks",
                "owner_name": "Priya Shah",
                "owner_email": None,
                "due_in_days": 5,
                "confidence_score": 0.76,
                "source_quote": "Let's find time next week to review the new dashboard mocks together.",
                "assign_to_self": False,
            },
        ],
    },
    {
        "title": "Customer Call — Acme Corp",
        "days_ago": 1,
        "source": "upload",
        "transcript_excerpt": (
            "...great questions. I'll shoot over our integration docs today. "
            "Someone will need to follow up on the SSO timeline question by "
            "early next week..."
        ),
        "commitments": [
            {
                "task_text": "Send Acme the integration docs",
                "owner_name": "You",
                "due_in_days": 0,
                "confidence_score": 0.91,
                "source_quote": "I'll shoot over our integration docs today.",
                "assign_to_self": True,
            },
            {
                "task_text": "Follow up on the SSO timeline question",
                "owner_name": "Priya Shah",
                "owner_email": None,
                "due_in_days": 4,
                "confidence_score": 0.82,
                "source_quote": "Someone will need to follow up on the SSO timeline question by early next week.",
                "assign_to_self": False,
            },
        ],
    },
]


@app.route("/api/meetings/seed-demo", methods=["POST"])
def seed_demo():
    email = require_email()
    if not email:
        return jsonify({"error": "not identified"}), 401

    db = get_db()
    created_meetings = 0
    created_commitments = 0
    for meeting in DEMO_MEETINGS:
        meeting_date = (date.today() - timedelta(days=meeting["days_ago"])).isoformat()
        cur = db.execute(
            "INSERT INTO meetings (owner_email, title, meeting_date, created_at, source, transcript_excerpt) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                email,
                meeting["title"],
                meeting_date,
                now_iso(),
                meeting["source"],
                meeting["transcript_excerpt"],
            ),
        )
        meeting_id = cur.lastrowid
        created_meetings += 1
        for c in meeting["commitments"]:
            due_date = (date.today() + timedelta(days=c["due_in_days"])).isoformat()
            owner_email = email if c.get("assign_to_self") else None
            db.execute(
                "INSERT INTO commitments "
                "(meeting_id, task_text, owner_name, owner_email, due_date, status, confidence_score, source_quote, created_at) "
                "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?)",
                (
                    meeting_id,
                    c["task_text"],
                    c["owner_name"],
                    owner_email,
                    due_date,
                    c["confidence_score"],
                    c["source_quote"],
                    now_iso(),
                ),
            )
            created_commitments += 1
    db.commit()
    db.close()
    return jsonify({"meetings_created": created_meetings, "commitments_created": created_commitments}), 201


# ---------- commitments ----------

def with_overdue_flag(row_dict):
    """Overdue is computed at read time from due_date, not stored as a status
    value — avoids needing a background job to flip statuses."""
    due = row_dict.get("due_date")
    is_overdue = bool(due) and due < today_str() and row_dict.get("status") != "done"
    row_dict["is_overdue"] = is_overdue
    return row_dict


@app.route("/api/commitments", methods=["GET"])
def list_commitments():
    email = require_email()
    if not email:
        return jsonify({"error": "not identified"}), 401

    status_filter = request.args.get("status")
    owner_filter = request.args.get("owner")

    query = (
        "SELECT commitments.*, meetings.title AS meeting_title "
        "FROM commitments JOIN meetings ON meetings.id = commitments.meeting_id "
        "WHERE meetings.owner_email = ?"
    )
    params = [email]
    if status_filter:
        query += " AND commitments.status = ?"
        params.append(status_filter)
    if owner_filter:
        query += " AND commitments.owner_name = ?"
        params.append(owner_filter)
    query += " ORDER BY commitments.due_date ASC"

    db = get_db()
    rows = db.execute(query, params).fetchall()
    db.close()
    return jsonify([with_overdue_flag(dict(r)) for r in rows])


def get_commitment_or_404(db, email, commitment_id):
    row = db.execute(
        "SELECT commitments.*, meetings.title AS meeting_title, meetings.owner_email AS meeting_owner_email "
        "FROM commitments JOIN meetings ON meetings.id = commitments.meeting_id "
        "WHERE commitments.id = ?",
        (commitment_id,),
    ).fetchone()
    if row is None or row["meeting_owner_email"] != email:
        return None
    return row


@app.route("/api/commitments/<int:commitment_id>", methods=["GET"])
def get_commitment(commitment_id):
    email = require_email()
    if not email:
        return jsonify({"error": "not identified"}), 401

    db = get_db()
    row = get_commitment_or_404(db, email, commitment_id)
    if row is None:
        db.close()
        return jsonify({"error": "not found"}), 404

    nudges = db.execute(
        "SELECT * FROM nudge_log WHERE commitment_id = ? ORDER BY triggered_at DESC",
        (commitment_id,),
    ).fetchall()
    tracker = db.execute(
        "SELECT provider, enabled FROM tracker_settings WHERE user_email = ?", (email,)
    ).fetchone()
    tracker_syncs = db.execute(
        "SELECT * FROM tracker_sync_log WHERE commitment_id = ? ORDER BY triggered_at DESC",
        (commitment_id,),
    ).fetchall()
    db.close()

    result = with_overdue_flag(dict(row))
    result["nudge_history"] = [dict(n) for n in nudges]
    result["tracker_configured"] = bool(tracker and tracker["provider"] != "none" and tracker["enabled"])
    result["tracker_provider"] = tracker["provider"] if tracker else "none"
    result["tracker_history"] = [dict(t) for t in tracker_syncs]
    return jsonify(result)


@app.route("/api/commitments/<int:commitment_id>", methods=["PATCH"])
def update_commitment(commitment_id):
    email = require_email()
    if not email:
        return jsonify({"error": "not identified"}), 401

    data = request.get_json(force=True)
    status = data.get("status")
    if status not in ("pending", "in_progress", "done"):
        return jsonify({"error": "status must be one of pending, in_progress, done"}), 400

    db = get_db()
    row = get_commitment_or_404(db, email, commitment_id)
    if row is None:
        db.close()
        return jsonify({"error": "not found"}), 404

    db.execute("UPDATE commitments SET status = ? WHERE id = ?", (status, commitment_id))
    db.commit()
    db.close()
    return jsonify({"ok": True})


@app.route("/api/commitments/<int:commitment_id>/send-test-nudge", methods=["POST"])
def send_test_nudge(commitment_id):
    """Mock only: writes a nudge_log row. No outbound HTTP request is made,
    regardless of what's saved in nudge_settings."""
    email = require_email()
    if not email:
        return jsonify({"error": "not identified"}), 401

    data = request.get_json(silent=True) or {}
    channel = data.get("channel", "slack")

    db = get_db()
    row = get_commitment_or_404(db, email, commitment_id)
    if row is None:
        db.close()
        return jsonify({"error": "not found"}), 404

    triggered_at = now_iso()
    db.execute(
        "INSERT INTO nudge_log (commitment_id, triggered_at, channel, status) VALUES (?, ?, ?, 'mock_sent')",
        (commitment_id, triggered_at, channel),
    )
    db.commit()
    db.close()
    return jsonify({"ok": True, "triggered_at": triggered_at, "channel": channel, "status": "mock_sent"}), 201


# ---------- nudge settings ----------

@app.route("/api/nudge-settings", methods=["GET"])
def list_nudge_settings():
    email = require_email()
    if not email:
        return jsonify({"error": "not identified"}), 401
    db = get_db()
    rows = db.execute(
        "SELECT * FROM nudge_settings WHERE user_email = ? ORDER BY channel", (email,)
    ).fetchall()
    db.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/nudge-settings", methods=["POST"])
def save_nudge_settings():
    email = require_email()
    if not email:
        return jsonify({"error": "not identified"}), 401

    data = request.get_json(force=True)
    channel = data.get("channel")
    webhook_or_key = (data.get("webhook_or_key") or "").strip()
    cadence = data.get("cadence", "day_before")
    enabled = 1 if data.get("enabled", True) else 0

    if channel not in ("slack", "teams", "email"):
        return jsonify({"error": "channel must be one of slack, teams, email"}), 400
    if not webhook_or_key:
        return jsonify({"error": "webhook_or_key is required"}), 400
    if cadence not in ("day_before", "on_due", "daily_after_overdue"):
        return jsonify({"error": "invalid cadence"}), 400

    db = get_db()
    db.execute(
        "INSERT INTO nudge_settings (user_email, channel, webhook_or_key, cadence, enabled) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(user_email, channel) DO UPDATE SET "
        "webhook_or_key = excluded.webhook_or_key, cadence = excluded.cadence, enabled = excluded.enabled",
        (email, channel, webhook_or_key, cadence, enabled),
    )
    db.commit()
    db.close()
    return jsonify({"ok": True}), 201


# ---------- external task tracker (Jira / Google Sheets) ----------

TRACKER_REQUIRED_FIELDS = {
    "jira": ["base_url", "project_key", "email", "api_token"],
    "google_sheets": ["spreadsheet_id", "api_key"],
}


@app.route("/api/tracker-settings", methods=["GET"])
def get_tracker_settings():
    email = require_email()
    if not email:
        return jsonify({"error": "not identified"}), 401
    db = get_db()
    row = db.execute(
        "SELECT provider, config_json, enabled FROM tracker_settings WHERE user_email = ?", (email,)
    ).fetchone()
    db.close()
    if row is None:
        return jsonify({"provider": "none", "config": {}, "enabled": True})
    return jsonify({
        "provider": row["provider"],
        "config": json.loads(row["config_json"]),
        "enabled": bool(row["enabled"]),
    })


@app.route("/api/tracker-settings", methods=["POST"])
def save_tracker_settings():
    email = require_email()
    if not email:
        return jsonify({"error": "not identified"}), 401

    data = request.get_json(force=True)
    provider = data.get("provider", "none")
    config = data.get("config") or {}
    enabled = 1 if data.get("enabled", True) else 0

    if provider not in ("none", "jira", "google_sheets"):
        return jsonify({"error": "provider must be one of none, jira, google_sheets"}), 400

    if provider != "none":
        missing = [f for f in TRACKER_REQUIRED_FIELDS[provider] if not (config.get(f) or "").strip()]
        if missing:
            return jsonify({"error": f"missing required field(s) for {provider}: {', '.join(missing)}"}), 400

    db = get_db()
    db.execute(
        "INSERT INTO tracker_settings (user_email, provider, config_json, enabled) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(user_email) DO UPDATE SET "
        "provider = excluded.provider, config_json = excluded.config_json, enabled = excluded.enabled",
        (email, provider, json.dumps(config), enabled),
    )
    db.commit()
    db.close()
    return jsonify({"ok": True}), 201


@app.route("/api/commitments/<int:commitment_id>/sync-tracker", methods=["POST"])
def sync_tracker(commitment_id):
    """Mock only: writes a tracker_sync_log row describing what would be sent.
    No real Jira/Google Sheets API call is made."""
    email = require_email()
    if not email:
        return jsonify({"error": "not identified"}), 401

    db = get_db()
    row = get_commitment_or_404(db, email, commitment_id)
    if row is None:
        db.close()
        return jsonify({"error": "not found"}), 404

    tracker = db.execute(
        "SELECT provider, config_json, enabled FROM tracker_settings WHERE user_email = ?", (email,)
    ).fetchone()
    if tracker is None or tracker["provider"] == "none" or not tracker["enabled"]:
        db.close()
        return jsonify({"error": "no task tracker configured — set one up in Integrations"}), 400

    provider = tracker["provider"]
    config = json.loads(tracker["config_json"])
    if provider == "jira":
        detail = f"Would create/update a Jira issue in project {config.get('project_key')} at {config.get('base_url')}"
    else:
        detail = f"Would append a row to Google Sheet {config.get('spreadsheet_id')}"

    triggered_at = now_iso()
    db.execute(
        "INSERT INTO tracker_sync_log (commitment_id, provider, triggered_at, status, detail) "
        "VALUES (?, ?, ?, 'mock_synced', ?)",
        (commitment_id, provider, triggered_at, detail),
    )
    db.commit()
    db.close()
    return jsonify({"ok": True, "triggered_at": triggered_at, "provider": provider, "status": "mock_synced", "detail": detail}), 201


if __name__ == "__main__":
    if not DB_PATH.exists():
        init_db()
        print(f"Initialized new database at {DB_PATH}")
    app.run(debug=True, port=5000)
