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

Tracker sync is two-way in shape, not just push: outbound ("sync to
tracker") is still mocked, but `/api/webhooks/tracker-update` is a *real*,
functional inbound endpoint — point a Jira Automation rule or a Google Apps
Script trigger at it and a status change made there updates the matching
commitment here. Receiving a webhook needs no outbound credentials, so it's
safe to make real even while the push direction stays mocked.
`simulate-inbound-update` exercises the same code path from the UI, so the
round trip is demonstrable without a live external caller.

`transcript_source_settings` is one level earlier in the pipeline: which
app (Zoom / Google Meet / Teams / Otter.ai) a user's transcripts come from.
"Check now" is still a mocked pull — it only logs what it would do. But
transcript *capture* also has a real push path: `/api/webhooks/transcript-
capture/<token>` is a real inbound endpoint (same shape as the tracker
webhook above) that creates a meeting the instant it's called, identified
by a per-user `webhook_token` rather than a session or plain email — a
Custom app's own automation, or (once wired up) Zoom's own event
subscription, can point at it directly. `simulate-incoming-capture`
exercises the same code path from the UI. There's still no transcript
parser wired in (that's being built separately), so a captured meeting
lands with raw transcript text only — commitments are still entered by
hand via `POST /api/commitments`, same as a manual upload — see readme.md.

Each of the three settings sections (nudges, tracker, transcript source) also
accepts a user-defined "custom" app, for when the predefined list doesn't
cover what someone actually uses. Custom apps get generic credential fields
(endpoint/webhook + API key) instead of provider-specific ones, and their
"how to get these details" steps are suggested via `/api/setup-steps/suggest`
instead of being hand-written — see that endpoint's docstring for how that
degrades gracefully with no API key configured.

Run:
    pip install flask
    python app.py
Then open http://localhost:5000 in a browser.
"""

import json
import os
import re
import secrets
import sqlite3
import urllib.error
import urllib.request
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


def migrate_db():
    """Adds columns introduced after a user's local DB was first created.
    SQLite has no 'ADD COLUMN IF NOT EXISTS', so check pragma table_info first."""
    if not DB_PATH.exists():
        return
    with sqlite3.connect(DB_PATH) as conn:
        for table in ("nudge_settings", "tracker_settings", "transcript_source_settings"):
            cols = {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}
            if "custom_name" not in cols:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN custom_name TEXT")

        source_cols = {row[1] for row in conn.execute("PRAGMA table_info(transcript_source_settings)")}
        if "webhook_token" not in source_cols:
            # SQLite's ALTER TABLE can't add a UNIQUE column directly; the
            # uniqueness constraint only applies to fresh installs via
            # schema.sql. Collisions are effectively impossible in practice
            # (secrets.token_urlsafe(24)), so this is an acceptable gap for
            # a migrated local DB.
            conn.execute("ALTER TABLE transcript_source_settings ADD COLUMN webhook_token TEXT")

        meeting_cols = {row[1] for row in conn.execute("PRAGMA table_info(meetings)")}
        if "transcript_provider" not in meeting_cols:
            conn.execute("ALTER TABLE meetings ADD COLUMN transcript_provider TEXT")

        log_cols = {row[1] for row in conn.execute("PRAGMA table_info(transcript_sync_log)")}
        if "direction" not in log_cols:
            conn.execute("ALTER TABLE transcript_sync_log ADD COLUMN direction TEXT NOT NULL DEFAULT 'outbound'")
        if "meeting_id" not in log_cols:
            conn.execute("ALTER TABLE transcript_sync_log ADD COLUMN meeting_id INTEGER REFERENCES meetings(id)")

        conn.commit()


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


def create_meeting_row(db, owner_email, title, meeting_date, transcript_excerpt, source, transcript_provider=None):
    """Single insert point for a meeting row — manual upload, every real
    capture webhook, and the simulate button all go through this."""
    cur = db.execute(
        "INSERT INTO meetings (owner_email, title, meeting_date, created_at, source, transcript_excerpt, transcript_provider) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (owner_email, title, meeting_date, now_iso(), source, transcript_excerpt, transcript_provider),
    )
    return cur.lastrowid


def log_inbound_capture(db, user_email, provider, meeting_id, detail):
    """Shared by every real/simulated transcript-capture path: records that
    a transcript was pushed to us (as opposed to 'check now''s outbound
    direction) and links it to the meeting it created."""
    db.execute(
        "INSERT INTO transcript_sync_log (user_email, provider, direction, triggered_at, status, detail, meeting_id) "
        "VALUES (?, ?, 'inbound', ?, 'received', ?, ?)",
        (user_email, provider, now_iso(), detail, meeting_id),
    )


@app.route("/api/meetings", methods=["POST"])
def create_meeting():
    """Manual transcript upload — the real path for getting a meeting into
    the app today, since the automated parser isn't wired in yet."""
    email = require_email()
    if not email:
        return jsonify({"error": "not identified"}), 401

    data = request.get_json(force=True)
    title = (data.get("title") or "").strip()
    meeting_date = (data.get("meeting_date") or "").strip() or today_str()
    transcript_excerpt = (data.get("transcript_excerpt") or "").strip() or None

    if not title:
        return jsonify({"error": "title is required"}), 400

    db = get_db()
    meeting_id = create_meeting_row(db, email, title, meeting_date, transcript_excerpt, source="upload")
    db.commit()
    db.close()
    return jsonify({"id": meeting_id}), 201


@app.route("/api/meetings/<int:meeting_id>", methods=["GET"])
def get_meeting(meeting_id):
    email = require_email()
    if not email:
        return jsonify({"error": "not identified"}), 401

    db = get_db()
    meeting = db.execute(
        "SELECT * FROM meetings WHERE id = ? AND owner_email = ?", (meeting_id, email)
    ).fetchone()
    if meeting is None:
        db.close()
        return jsonify({"error": "not found"}), 404

    commitments = db.execute(
        "SELECT * FROM commitments WHERE meeting_id = ? ORDER BY due_date ASC", (meeting_id,)
    ).fetchall()
    db.close()

    result = dict(meeting)
    result["commitments"] = [with_overdue_flag(dict(c)) for c in commitments]
    return jsonify(result)


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


@app.route("/api/commitments", methods=["POST"])
def create_commitment():
    """Manual commitment entry, scoped to a meeting the user owns. Fills the
    gap until the real transcript parser is wired in — confidence_score is
    left NULL to distinguish a hand-entered commitment from an extracted
    one in the UI."""
    email = require_email()
    if not email:
        return jsonify({"error": "not identified"}), 401

    data = request.get_json(force=True)
    meeting_id = data.get("meeting_id")
    task_text = (data.get("task_text") or "").strip()
    owner_name = (data.get("owner_name") or "").strip()
    owner_email = (data.get("owner_email") or "").strip() or None
    due_date = (data.get("due_date") or "").strip() or None
    source_quote = (data.get("source_quote") or "").strip() or None

    if not task_text or not owner_name:
        return jsonify({"error": "task_text and owner_name are required"}), 400

    db = get_db()
    meeting = db.execute(
        "SELECT id FROM meetings WHERE id = ? AND owner_email = ?", (meeting_id, email)
    ).fetchone()
    if meeting is None:
        db.close()
        return jsonify({"error": "meeting not found"}), 404

    cur = db.execute(
        "INSERT INTO commitments "
        "(meeting_id, task_text, owner_name, owner_email, due_date, status, confidence_score, source_quote, created_at) "
        "VALUES (?, ?, ?, ?, ?, 'pending', NULL, ?, ?)",
        (meeting_id, task_text, owner_name, owner_email, due_date, source_quote, now_iso()),
    )
    db.commit()
    db.close()
    return jsonify({"id": cur.lastrowid}), 201


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
    custom_name = (data.get("custom_name") or "").strip() or None
    webhook_or_key = (data.get("webhook_or_key") or "").strip()
    cadence = data.get("cadence", "day_before")
    enabled = 1 if data.get("enabled", True) else 0

    if channel not in ("slack", "teams", "email", "custom"):
        return jsonify({"error": "channel must be one of slack, teams, email, custom"}), 400
    if channel == "custom" and not custom_name:
        return jsonify({"error": "custom_name is required for a custom channel"}), 400
    if not webhook_or_key:
        return jsonify({"error": "webhook_or_key is required"}), 400
    if cadence not in ("day_before", "on_due", "daily_after_overdue"):
        return jsonify({"error": "invalid cadence"}), 400

    db = get_db()
    db.execute(
        "INSERT INTO nudge_settings (user_email, channel, custom_name, webhook_or_key, cadence, enabled) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(user_email, channel) DO UPDATE SET "
        "custom_name = excluded.custom_name, webhook_or_key = excluded.webhook_or_key, "
        "cadence = excluded.cadence, enabled = excluded.enabled",
        (email, channel, custom_name, webhook_or_key, cadence, enabled),
    )
    db.commit()
    db.close()
    return jsonify({"ok": True}), 201


# ---------- external task tracker (Jira / Google Sheets) ----------

TRACKER_REQUIRED_FIELDS = {
    "jira": ["base_url", "project_key", "email", "api_token"],
    "google_sheets": ["spreadsheet_id", "api_key"],
    "custom": ["endpoint", "api_key"],
}


@app.route("/api/tracker-settings", methods=["GET"])
def get_tracker_settings():
    email = require_email()
    if not email:
        return jsonify({"error": "not identified"}), 401
    db = get_db()
    row = db.execute(
        "SELECT provider, custom_name, config_json, enabled FROM tracker_settings WHERE user_email = ?", (email,)
    ).fetchone()
    db.close()
    if row is None:
        return jsonify({"provider": "none", "custom_name": None, "config": {}, "enabled": True})
    return jsonify({
        "provider": row["provider"],
        "custom_name": row["custom_name"],
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
    custom_name = (data.get("custom_name") or "").strip() or None
    config = data.get("config") or {}
    enabled = 1 if data.get("enabled", True) else 0

    if provider not in ("none", "jira", "google_sheets", "custom"):
        return jsonify({"error": "provider must be one of none, jira, google_sheets, custom"}), 400
    if provider == "custom" and not custom_name:
        return jsonify({"error": "custom_name is required for a custom tracker"}), 400

    if provider != "none":
        missing = [f for f in TRACKER_REQUIRED_FIELDS[provider] if not (config.get(f) or "").strip()]
        if missing:
            return jsonify({"error": f"missing required field(s) for {provider}: {', '.join(missing)}"}), 400

    db = get_db()
    db.execute(
        "INSERT INTO tracker_settings (user_email, provider, custom_name, config_json, enabled) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(user_email) DO UPDATE SET "
        "provider = excluded.provider, custom_name = excluded.custom_name, "
        "config_json = excluded.config_json, enabled = excluded.enabled",
        (email, provider, custom_name, json.dumps(config), enabled),
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
        "SELECT provider, custom_name, config_json, enabled FROM tracker_settings WHERE user_email = ?", (email,)
    ).fetchone()
    if tracker is None or tracker["provider"] == "none" or not tracker["enabled"]:
        db.close()
        return jsonify({"error": "no task tracker configured — set one up in Integrations"}), 400

    provider = tracker["provider"]
    config = json.loads(tracker["config_json"])

    # First sync mints a fake external reference (a real integration would get
    # this back from the Jira/Sheets/custom API call) so the inbound side has
    # something to match a later status update against.
    external_ref = row["external_ref"]
    if not external_ref:
        if provider == "jira":
            external_ref = f"{config.get('project_key', 'TASK')}-{commitment_id}"
        elif provider == "custom":
            external_ref = f"ext-{commitment_id}"
        else:
            external_ref = f"row-{commitment_id}"
        db.execute("UPDATE commitments SET external_ref = ? WHERE id = ?", (external_ref, commitment_id))

    if provider == "jira":
        detail = f"Would create/update Jira issue {external_ref} in project {config.get('project_key')} at {config.get('base_url')}"
    elif provider == "custom":
        detail = f"Would send {external_ref} to {tracker['custom_name'] or 'your custom tracker'} at {config.get('endpoint')}"
    else:
        detail = f"Would write {external_ref} to Google Sheet {config.get('spreadsheet_id')}"

    triggered_at = now_iso()
    db.execute(
        "INSERT INTO tracker_sync_log (commitment_id, provider, direction, triggered_at, status, detail) "
        "VALUES (?, ?, 'outbound', ?, 'mock_synced', ?)",
        (commitment_id, provider, triggered_at, detail),
    )
    db.commit()
    db.close()
    return jsonify({
        "ok": True, "triggered_at": triggered_at, "provider": provider,
        "status": "mock_synced", "detail": detail, "external_ref": external_ref,
    }), 201


def apply_inbound_tracker_update(db, commitment_id, new_status, provider, source_label):
    """Shared by the real webhook and the UI's simulate button: applies a
    status change that originated in the external tracker, and logs it as
    an inbound sync event (as opposed to the outbound 'push' direction)."""
    if new_status not in ("pending", "in_progress", "done"):
        return None, "status must be one of pending, in_progress, done"

    db.execute("UPDATE commitments SET status = ? WHERE id = ?", (new_status, commitment_id))
    triggered_at = now_iso()
    detail = f"Status changed to '{new_status}' via {source_label}"
    db.execute(
        "INSERT INTO tracker_sync_log (commitment_id, provider, direction, triggered_at, status, detail) "
        "VALUES (?, ?, 'inbound', ?, 'received', ?)",
        (commitment_id, provider, triggered_at, detail),
    )
    return {"triggered_at": triggered_at, "detail": detail}, None


@app.route("/api/commitments/<int:commitment_id>/simulate-inbound-update", methods=["POST"])
def simulate_inbound_update(commitment_id):
    """UI test button: exercises the same code path a real inbound webhook
    call would, so two-way sync is demonstrable without a live Jira/Sheets
    automation calling in. Session-authenticated, unlike the public webhook
    below."""
    email = require_email()
    if not email:
        return jsonify({"error": "not identified"}), 401

    data = request.get_json(force=True)
    new_status = data.get("status")

    db = get_db()
    row = get_commitment_or_404(db, email, commitment_id)
    if row is None:
        db.close()
        return jsonify({"error": "not found"}), 404
    if not row["external_ref"]:
        db.close()
        return jsonify({"error": "sync to the tracker at least once first — nothing to simulate an update against yet"}), 400

    tracker = db.execute(
        "SELECT provider, custom_name FROM tracker_settings WHERE user_email = ?", (email,)
    ).fetchone()
    provider = tracker["provider"] if tracker else "tracker"
    if provider == "custom":
        label = (tracker["custom_name"] if tracker else None) or "your custom tracker"
    else:
        label = {"jira": "Jira", "google_sheets": "Google Sheets"}.get(provider, provider)

    result, error = apply_inbound_tracker_update(db, commitment_id, new_status, provider, f"{label} (simulated)")
    if error:
        db.close()
        return jsonify({"error": error}), 400
    db.commit()
    db.close()
    return jsonify({"ok": True, **result}), 201


@app.route("/api/webhooks/tracker-update", methods=["POST"])
def tracker_webhook():
    """Real, functional inbound endpoint — the two-way half of the tracker
    integration. Point a Jira Automation rule or a Google Apps Script
    trigger at this URL when a task's status changes there, and the
    matching commitment here gets updated. No outbound call is made
    anywhere in this app; this route only *receives*.

    Not session-protected — an external service has no browser session —
    so the commitment is identified via `email` + `external_ref` in the
    body instead. Intentionally simple for a prototype with no public
    deployment yet; a real deployment would want a shared secret or
    signature check here.
    """
    data = request.get_json(force=True)
    email = (data.get("email") or "").strip().lower()
    external_ref = (data.get("external_ref") or "").strip()
    new_status = data.get("status")
    provider = data.get("provider", "external")

    if not email or not external_ref:
        return jsonify({"error": "email and external_ref are required"}), 400

    db = get_db()
    row = db.execute(
        "SELECT commitments.id FROM commitments JOIN meetings ON meetings.id = commitments.meeting_id "
        "WHERE meetings.owner_email = ? AND commitments.external_ref = ?",
        (email, external_ref),
    ).fetchone()
    if row is None:
        db.close()
        return jsonify({"error": "no matching commitment for that email/external_ref"}), 404

    result, error = apply_inbound_tracker_update(db, row["id"], new_status, provider, provider)
    if error:
        db.close()
        return jsonify({"error": error}), 400
    db.commit()
    db.close()
    return jsonify({"ok": True, "commitment_id": row["id"], **result}), 201


# ---------- transcript source (Zoom / Google Meet / Teams / Otter.ai) ----------

TRANSCRIPT_SOURCE_REQUIRED_FIELDS = {
    "zoom": ["account_id", "client_id", "client_secret", "webhook_secret_token"],
    "google_meet": ["workspace_email", "api_key"],
    "teams": ["tenant_id", "api_key"],
    "otter": ["account_email", "api_key"],
    "custom": ["account_identifier", "api_key"],
}
TRANSCRIPT_SOURCE_LABELS = {
    "zoom": "Zoom", "google_meet": "Google Meet", "teams": "Microsoft Teams", "otter": "Otter.ai",
}


def get_or_create_webhook_token(db, email):
    """Every transcript source row gets a webhook_token as soon as it
    exists, regardless of provider — it's what a real Zoom/Meet/Teams
    integration (or the generic capture endpoint) uses in its callback URL
    to identify which user's row to act on, instead of a plain email match."""
    row = db.execute("SELECT webhook_token FROM transcript_source_settings WHERE user_email = ?", (email,)).fetchone()
    if row and row["webhook_token"]:
        return row["webhook_token"]
    token = secrets.token_urlsafe(24)
    if row:
        db.execute("UPDATE transcript_source_settings SET webhook_token = ? WHERE user_email = ?", (token, email))
    else:
        db.execute(
            "INSERT INTO transcript_source_settings (user_email, webhook_token) VALUES (?, ?)",
            (email, token),
        )
    return token


@app.route("/api/transcript-source-settings", methods=["GET"])
def get_transcript_source_settings():
    email = require_email()
    if not email:
        return jsonify({"error": "not identified"}), 401
    db = get_db()
    row = db.execute(
        "SELECT provider, custom_name, config_json, enabled, webhook_token FROM transcript_source_settings WHERE user_email = ?", (email,)
    ).fetchone()
    if row is None:
        token = get_or_create_webhook_token(db, email)
        db.commit()
        db.close()
        return jsonify({"provider": "none", "custom_name": None, "config": {}, "enabled": True, "webhook_token": token})
    db.close()
    return jsonify({
        "provider": row["provider"],
        "custom_name": row["custom_name"],
        "config": json.loads(row["config_json"]),
        "enabled": bool(row["enabled"]),
        "webhook_token": row["webhook_token"],
    })


@app.route("/api/transcript-source-settings", methods=["POST"])
def save_transcript_source_settings():
    email = require_email()
    if not email:
        return jsonify({"error": "not identified"}), 401

    data = request.get_json(force=True)
    provider = data.get("provider", "none")
    custom_name = (data.get("custom_name") or "").strip() or None
    config = data.get("config") or {}
    enabled = 1 if data.get("enabled", True) else 0

    if provider not in ("none", "zoom", "google_meet", "teams", "otter", "custom"):
        return jsonify({"error": "provider must be one of none, zoom, google_meet, teams, otter, custom"}), 400
    if provider == "custom" and not custom_name:
        return jsonify({"error": "custom_name is required for a custom transcript source"}), 400
    if provider != "none":
        missing = [f for f in TRANSCRIPT_SOURCE_REQUIRED_FIELDS[provider] if not (config.get(f) or "").strip()]
        if missing:
            return jsonify({"error": f"missing required field(s) for {provider}: {', '.join(missing)}"}), 400

    db = get_db()
    token = get_or_create_webhook_token(db, email)
    db.execute(
        "INSERT INTO transcript_source_settings (user_email, provider, custom_name, config_json, enabled, webhook_token) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(user_email) DO UPDATE SET "
        "provider = excluded.provider, custom_name = excluded.custom_name, "
        "config_json = excluded.config_json, enabled = excluded.enabled",
        (email, provider, custom_name, json.dumps(config), enabled, token),
    )
    db.commit()
    db.close()
    return jsonify({"ok": True, "webhook_token": token}), 201


@app.route("/api/transcript-source/regenerate-token", methods=["POST"])
def regenerate_transcript_webhook_token():
    """Rotates the webhook token — any URL built from the old one (pasted
    into a Zoom/Meet/Teams config, or used for the generic endpoint) stops
    working immediately."""
    email = require_email()
    if not email:
        return jsonify({"error": "not identified"}), 401
    db = get_db()
    token = secrets.token_urlsafe(24)
    cur = db.execute("UPDATE transcript_source_settings SET webhook_token = ? WHERE user_email = ?", (token, email))
    if cur.rowcount == 0:
        db.execute("INSERT INTO transcript_source_settings (user_email, webhook_token) VALUES (?, ?)", (email, token))
    db.commit()
    db.close()
    return jsonify({"ok": True, "webhook_token": token}), 201


@app.route("/api/transcript-source/check-now", methods=["POST"])
def check_transcript_source():
    """Mock only: logs what checking for new transcripts would do. No real
    Zoom/Meet/Teams/Otter API call is made — and even if it were, there's no
    parser yet on this end to turn a transcript into commitments. Upload one
    by hand in the meantime (POST /api/meetings)."""
    email = require_email()
    if not email:
        return jsonify({"error": "not identified"}), 401

    db = get_db()
    source = db.execute(
        "SELECT provider, custom_name, config_json, enabled FROM transcript_source_settings WHERE user_email = ?", (email,)
    ).fetchone()
    if source is None or source["provider"] == "none" or not source["enabled"]:
        db.close()
        return jsonify({"error": "no transcript source configured — set one up below, or upload a transcript manually"}), 400

    provider = source["provider"]
    config = json.loads(source["config_json"])
    account = (
        config.get("account_id") or config.get("account_email") or config.get("workspace_email")
        or config.get("tenant_id") or config.get("account_identifier") or "your account"
    )
    label = source["custom_name"] if provider == "custom" else TRANSCRIPT_SOURCE_LABELS[provider]
    detail = f"Would check {label} ({account}) for new meeting transcripts"

    triggered_at = now_iso()
    db.execute(
        "INSERT INTO transcript_sync_log (user_email, provider, direction, triggered_at, status, detail) "
        "VALUES (?, ?, 'outbound', ?, 'mock_checked', ?)",
        (email, provider, triggered_at, detail),
    )
    db.commit()
    db.close()
    return jsonify({"ok": True, "triggered_at": triggered_at, "provider": provider, "status": "mock_checked", "detail": detail}), 201


@app.route("/api/transcript-source/log", methods=["GET"])
def transcript_source_log():
    email = require_email()
    if not email:
        return jsonify({"error": "not identified"}), 401
    db = get_db()
    rows = db.execute(
        "SELECT transcript_sync_log.*, meetings.title AS meeting_title "
        "FROM transcript_sync_log LEFT JOIN meetings ON meetings.id = transcript_sync_log.meeting_id "
        "WHERE transcript_sync_log.user_email = ? ORDER BY transcript_sync_log.triggered_at DESC LIMIT 10",
        (email,),
    ).fetchall()
    db.close()
    return jsonify([dict(r) for r in rows])


# ---------- live transcript capture (real inbound + simulate) ----------

LIVE_CAPTURE_DEMO = {
    "title": "Weekly Standup — Growth Team",
    "transcript_excerpt": (
        "...alright, quick sync. I'll get the campaign brief over to marketing "
        "by tomorrow. And I'll follow up with the vendor on the contract "
        "redlines before Friday. Let's regroup once we hear back..."
    ),
}


@app.route("/api/webhooks/transcript-capture/<token>", methods=["POST"])
def transcript_capture_webhook(token):
    """Real, functional inbound endpoint — the generic push half of
    transcript sourcing, for Custom apps or anything that can be pointed at
    a plain JSON webhook. Point it at this URL (with your token from
    Integrations) and a new meeting is created here immediately, no manual
    upload needed. No outbound call is made anywhere in this app; this
    route only *receives*.

    Not session-protected — an external service has no browser session —
    so the user is identified via the webhook_token in the URL rather than
    a session cookie or a plain email field."""
    db = get_db()
    source = db.execute(
        "SELECT user_email, provider FROM transcript_source_settings WHERE webhook_token = ?", (token,)
    ).fetchone()
    if source is None:
        db.close()
        return jsonify({"error": "unrecognized webhook token"}), 404

    data = request.get_json(force=True)
    title = (data.get("title") or "").strip()
    meeting_date = (data.get("meeting_date") or "").strip() or today_str()
    transcript_excerpt = (data.get("transcript_excerpt") or "").strip() or None
    provider = (data.get("provider") or "").strip() or source["provider"] or "custom"

    if not title:
        db.close()
        return jsonify({"error": "title is required"}), 400

    meeting_id = create_meeting_row(db, source["user_email"], title, meeting_date, transcript_excerpt, source="live", transcript_provider=provider)
    log_inbound_capture(db, source["user_email"], provider, meeting_id, f'Received live transcript for "{title}"')
    db.commit()
    db.close()
    return jsonify({"ok": True, "meeting_id": meeting_id}), 201


@app.route("/api/meetings/simulate-incoming-capture", methods=["POST"])
def simulate_incoming_capture():
    """UI test button: exercises the same code path a real transcript-
    capture webhook call would, so the push flow (and the dashboard's live
    update) is demonstrable without a live Zoom/Meet/Teams automation
    calling in."""
    email = require_email()
    if not email:
        return jsonify({"error": "not identified"}), 401

    db = get_db()
    source = db.execute(
        "SELECT provider, custom_name, enabled FROM transcript_source_settings WHERE user_email = ?", (email,)
    ).fetchone()
    if source is None or source["provider"] == "none" or not source["enabled"]:
        db.close()
        return jsonify({"error": "no transcript source configured — set one up below to simulate a live capture"}), 400

    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip() or LIVE_CAPTURE_DEMO["title"]
    meeting_date = (data.get("meeting_date") or "").strip() or today_str()
    transcript_excerpt = (data.get("transcript_excerpt") or "").strip() or LIVE_CAPTURE_DEMO["transcript_excerpt"]

    provider = source["provider"]
    label = source["custom_name"] if provider == "custom" else TRANSCRIPT_SOURCE_LABELS.get(provider, provider)

    meeting_id = create_meeting_row(db, email, title, meeting_date, transcript_excerpt, source="live", transcript_provider=provider)
    log_inbound_capture(db, email, provider, meeting_id, f'Simulated: {label} pushed a transcript for "{title}" the moment the meeting ended')
    db.commit()
    db.close()
    return jsonify({"ok": True, "meeting_id": meeting_id}), 201


# ---------- AI-suggested setup steps (custom, user-defined apps only) ----------
#
# The predefined providers above (Slack, Jira, Zoom, ...) have hand-written
# "how to get these details" steps in the frontend. A custom app the user
# types in has no such curated list, so this endpoint asks an LLM to guess a
# plausible one instead. This is explicitly best-effort and unverified —
# every response is labeled with a disclaimer, and the frontend renders it
# distinctly from the curated provider steps.
#
# Kept optional so `pip install flask` + `python app.py` still works with no
# API key (NFR3): if ANTHROPIC_API_KEY isn't set, or the call fails for any
# reason, a generic template list is returned instead — still disclaimed,
# just less specific. No new pip dependency is added; the call is made with
# the stdlib (urllib) rather than the anthropic package.

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY")
ANTHROPIC_MODEL = os.environ.get("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
ANTHROPIC_MESSAGES_URL = "https://api.anthropic.com/v1/messages"

SETUP_STEPS_CATEGORY_CONTEXT = {
    "nudge": "sending automated nudge/reminder notifications (like a Slack or Teams webhook)",
    "tracker": "syncing tasks/issues to and from an external task tracker (like Jira or a spreadsheet)",
    "transcript": "fetching meeting transcripts from a video-call or note-taking app (like Zoom or Otter.ai)",
}

AI_STEPS_DISCLAIMER = (
    "These setup steps are AI-generated, not verified by us, and may be inaccurate or "
    "out of date for this specific app. Double-check them against the app's own "
    "documentation before entering any credentials."
)


def _template_setup_steps(app_name, category):
    """Generic fallback used when no ANTHROPIC_API_KEY is configured, or the
    live call fails for any reason."""
    context = SETUP_STEPS_CATEGORY_CONTEXT.get(category, "connecting this app")
    return [
        f"Log in to {app_name} and look for a \"Settings\", \"Developer\", \"Integrations\", or \"API\" section.",
        f"Look for a way to create a webhook URL or an API key/token intended for {context}.",
        "Give the credential a descriptive name and the narrowest scope/permissions available.",
        f"Copy the generated URL or key and paste it into the field below — {app_name} won't show it again.",
        "Save here, then use the section's test/check action to confirm it's wired up.",
    ]


def _ai_setup_steps(app_name, category):
    """Returns a list of steps from a live Claude call, or None on any failure
    (missing key, network error, bad response shape, etc.) so the caller can
    fall back to the template list."""
    if not ANTHROPIC_API_KEY:
        return None

    context = SETUP_STEPS_CATEGORY_CONTEXT.get(category, "connecting this app to another tool")
    prompt = (
        f"A user wants to connect a third-party app called \"{app_name}\" to their own tool, "
        f"for the purpose of {context}. List 3-6 short, concrete steps for where in {app_name} "
        "they'd typically find or generate the webhook URL / API key they'd need. "
        "Reply with ONLY a plain numbered list, one short step per line, no preamble, "
        "no markdown formatting, no closing remarks. If you aren't confident about this "
        "specific app, give your best generic guess for a typical SaaS app instead of refusing."
    )

    body = json.dumps({
        "model": ANTHROPIC_MODEL,
        "max_tokens": 400,
        "messages": [{"role": "user", "content": prompt}],
    }).encode("utf-8")

    req = urllib.request.Request(
        ANTHROPIC_MESSAGES_URL,
        data=body,
        method="POST",
        headers={
            "content-type": "application/json",
            "x-api-key": ANTHROPIC_API_KEY,
            "anthropic-version": "2023-06-01",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        text = "".join(block.get("text", "") for block in payload.get("content", []))
        steps = []
        for line in text.splitlines():
            cleaned = re.sub(r"^\s*(?:[-*]|\d+[.)])\s*", "", line).strip()
            if cleaned:
                steps.append(cleaned)
        return steps or None
    except (urllib.error.URLError, TimeoutError, ValueError, KeyError, OSError):
        return None


@app.route("/api/setup-steps/suggest", methods=["POST"])
def suggest_setup_steps():
    email = require_email()
    if not email:
        return jsonify({"error": "not identified"}), 401

    data = request.get_json(force=True)
    app_name = (data.get("app_name") or "").strip()
    category = data.get("category")

    if category not in SETUP_STEPS_CATEGORY_CONTEXT:
        return jsonify({"error": "category must be one of nudge, tracker, transcript"}), 400
    if not app_name:
        return jsonify({"error": "app_name is required"}), 400

    steps = _ai_setup_steps(app_name, category)
    source = "ai"
    if steps is None:
        steps = _template_setup_steps(app_name, category)
        source = "template"

    return jsonify({"steps": steps, "source": source, "disclaimer": AI_STEPS_DISCLAIMER})


if __name__ == "__main__":
    if not DB_PATH.exists():
        init_db()
        print(f"Initialized new database at {DB_PATH}")
    migrate_db()
    app.run(debug=True, port=5000)
