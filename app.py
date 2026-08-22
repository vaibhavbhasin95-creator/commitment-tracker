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

`transcript_source_settings` is the same mock pattern again, one level
earlier in the pipeline: which app (Zoom / Google Meet / Teams / Otter.ai)
a user's transcripts *would* come from. "Check now" only logs what it would
do — there's no real transcript parser wired in yet (that's being built
separately). Until there is, meetings and their commitments can be entered
by hand via `POST /api/meetings` (paste/upload a transcript) and
`POST /api/commitments` (add a commitment to it) — see readme.md.

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
    cur = db.execute(
        "INSERT INTO meetings (owner_email, title, meeting_date, created_at, source, transcript_excerpt) "
        "VALUES (?, ?, ?, ?, 'upload', ?)",
        (email, title, meeting_date, now_iso(), transcript_excerpt),
    )
    meeting_id = cur.lastrowid
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

    # First sync mints a fake external reference (a real integration would get
    # this back from the Jira/Sheets API call) so the inbound side has
    # something to match a later status update against.
    external_ref = row["external_ref"]
    if not external_ref:
        external_ref = (
            f"{config.get('project_key', 'TASK')}-{commitment_id}"
            if provider == "jira"
            else f"row-{commitment_id}"
        )
        db.execute("UPDATE commitments SET external_ref = ? WHERE id = ?", (external_ref, commitment_id))

    if provider == "jira":
        detail = f"Would create/update Jira issue {external_ref} in project {config.get('project_key')} at {config.get('base_url')}"
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
        "SELECT provider FROM tracker_settings WHERE user_email = ?", (email,)
    ).fetchone()
    provider = tracker["provider"] if tracker else "tracker"
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
    "zoom": ["account_email", "api_key"],
    "google_meet": ["workspace_email", "api_key"],
    "teams": ["tenant_id", "api_key"],
    "otter": ["account_email", "api_key"],
}
TRANSCRIPT_SOURCE_LABELS = {
    "zoom": "Zoom", "google_meet": "Google Meet", "teams": "Microsoft Teams", "otter": "Otter.ai",
}


@app.route("/api/transcript-source-settings", methods=["GET"])
def get_transcript_source_settings():
    email = require_email()
    if not email:
        return jsonify({"error": "not identified"}), 401
    db = get_db()
    row = db.execute(
        "SELECT provider, config_json, enabled FROM transcript_source_settings WHERE user_email = ?", (email,)
    ).fetchone()
    db.close()
    if row is None:
        return jsonify({"provider": "none", "config": {}, "enabled": True})
    return jsonify({
        "provider": row["provider"],
        "config": json.loads(row["config_json"]),
        "enabled": bool(row["enabled"]),
    })


@app.route("/api/transcript-source-settings", methods=["POST"])
def save_transcript_source_settings():
    email = require_email()
    if not email:
        return jsonify({"error": "not identified"}), 401

    data = request.get_json(force=True)
    provider = data.get("provider", "none")
    config = data.get("config") or {}
    enabled = 1 if data.get("enabled", True) else 0

    if provider not in ("none", "zoom", "google_meet", "teams", "otter"):
        return jsonify({"error": "provider must be one of none, zoom, google_meet, teams, otter"}), 400
    if provider != "none":
        missing = [f for f in TRANSCRIPT_SOURCE_REQUIRED_FIELDS[provider] if not (config.get(f) or "").strip()]
        if missing:
            return jsonify({"error": f"missing required field(s) for {provider}: {', '.join(missing)}"}), 400

    db = get_db()
    db.execute(
        "INSERT INTO transcript_source_settings (user_email, provider, config_json, enabled) "
        "VALUES (?, ?, ?, ?) "
        "ON CONFLICT(user_email) DO UPDATE SET "
        "provider = excluded.provider, config_json = excluded.config_json, enabled = excluded.enabled",
        (email, provider, json.dumps(config), enabled),
    )
    db.commit()
    db.close()
    return jsonify({"ok": True}), 201


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
        "SELECT provider, config_json, enabled FROM transcript_source_settings WHERE user_email = ?", (email,)
    ).fetchone()
    if source is None or source["provider"] == "none" or not source["enabled"]:
        db.close()
        return jsonify({"error": "no transcript source configured — set one up below, or upload a transcript manually"}), 400

    provider = source["provider"]
    config = json.loads(source["config_json"])
    account = config.get("account_email") or config.get("workspace_email") or config.get("tenant_id") or "your account"
    detail = f"Would check {TRANSCRIPT_SOURCE_LABELS[provider]} ({account}) for new meeting transcripts"

    triggered_at = now_iso()
    db.execute(
        "INSERT INTO transcript_sync_log (user_email, provider, triggered_at, status, detail) "
        "VALUES (?, ?, ?, 'mock_checked', ?)",
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
        "SELECT * FROM transcript_sync_log WHERE user_email = ? ORDER BY triggered_at DESC LIMIT 10", (email,)
    ).fetchall()
    db.close()
    return jsonify([dict(r) for r in rows])


if __name__ == "__main__":
    if not DB_PATH.exists():
        init_db()
        print(f"Initialized new database at {DB_PATH}")
    app.run(debug=True, port=5000)
