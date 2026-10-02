"""
Meeting Commitment Tracker — local prototype backend.

Meetings arrive by manual upload or a live-capture webhook (see below), and
every real arrival path runs the trained ACTION classifier in
transcript_parser.py (auto_extract_commitments) over the transcript text to
populate commitments automatically — no hand-entry needed for a real
transcript. `POST /api/commitments` still exists for adding one by hand
(e.g. something the classifier missed, or a meeting with no saved
transcript), and demo meetings still use fixed illustrative data rather than
running through the classifier. This app tracks status and (mock) nudges for
whatever commitments end up attached to a meeting, however they got there.

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
exercises the same code path from the UI. A captured meeting runs through
the same auto-extraction as a manual upload — see readme.md.

Each of the three settings sections (nudges, tracker, transcript source) also
accepts a user-defined "custom" app, for when the predefined list doesn't
cover what someone actually uses. Custom apps get generic credential fields
(endpoint/webhook + API key) instead of provider-specific ones, and their
"how to get these details" steps are suggested via `/api/setup-steps/suggest`
instead of being hand-written — see that endpoint's docstring for how that
degrades gracefully with no API key configured.

Run:
    pip install -r requirements.txt
    python -m spacy download en_core_web_sm
    python ml/train_action_detector.py   # one-time: trains ml/action_detector.keras
    python app.py
Then open http://localhost:5000 in a browser. If the model hasn't been
trained yet, the app still runs fine — uploads/captures just come in with
no auto-extracted commitments (add them by hand, or train the model and hit
POST /api/meetings/<id>/extract-commitments to backfill).
"""

import base64
import hashlib
import hmac
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

import notifications
import people_directory

try:
    import transcript_parser
except ImportError:
    # Commitment extraction's dependencies (pandas, tensorflow, spacy, ...)
    # aren't installed — see requirements.txt / NFR3. The app still runs;
    # auto_extract_commitments() below just reports it as unavailable
    # instead of extracting anything.
    transcript_parser = None

try:
    import google_sheets
except ImportError:
    # google-api-python-client / google-auth-oauthlib aren't installed —
    # see requirements.txt. sync_tracker() reports Google Sheets as
    # unavailable instead of erroring.
    google_sheets = None

# Single source of truth for the app version — bump it together with a new
# CHANGELOG.md entry (see "How versioning works" there).
APP_VERSION = "0.7.3"

BASE_DIR = Path(__file__).parent
DB_PATH = BASE_DIR / "commitment_tracker.db"
SCHEMA_PATH = BASE_DIR / "schema.sql"

app = Flask(__name__, static_folder="static", static_url_path="")
app.secret_key = "dev-only-secret-change-before-any-real-deployment"


def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
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
        # Every statement in schema.sql is CREATE ... IF NOT EXISTS, so re-running
        # it adds tables introduced after this DB was created (e.g. people).
        conn.executescript(SCHEMA_PATH.read_text())

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

        # 0.6.0: people directory matching + real nudges
        commitment_cols = {row[1] for row in conn.execute("PRAGMA table_info(commitments)")}
        if "owner_match" not in commitment_cols:
            conn.execute("ALTER TABLE commitments ADD COLUMN owner_match TEXT")
        nudge_setting_cols = {row[1] for row in conn.execute("PRAGMA table_info(nudge_settings)")}
        if "config_json" not in nudge_setting_cols:
            conn.execute("ALTER TABLE nudge_settings ADD COLUMN config_json TEXT NOT NULL DEFAULT '{}'")
        nudge_log_cols = {row[1] for row in conn.execute("PRAGMA table_info(nudge_log)")}
        for col in ("reason", "detail"):
            if col not in nudge_log_cols:
                conn.execute(f"ALTER TABLE nudge_log ADD COLUMN {col} TEXT")

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


@app.route("/people")
def people_page():
    return send_from_directory(app.static_folder, "people.html")


# ---------- people directory API ----------

def _person_fields(data):
    person = {
        "first_name": (data.get("first_name") or "").strip(),
        "last_name": (data.get("last_name") or "").strip(),
        "email": (data.get("email") or "").strip().lower(),
        "username": (data.get("username") or "").strip(),
    }
    if not people_directory.EMAIL_RE.match(person["email"]):
        return None, "a valid email is required"
    if not (person["first_name"] or person["last_name"] or person["username"]):
        return None, "add at least a first name, last name or user name"
    return person, None


def _after_directory_change(db, email):
    counts = rematch_owners(db, email)
    db.commit()
    return counts


@app.route("/api/people", methods=["GET"])
def list_people():
    email = require_email()
    if not email:
        return jsonify({"error": "not identified"}), 401
    db = get_db()
    people = load_people(db, email)
    tagged = db.execute(
        "SELECT commitments.owner_match, COUNT(*) AS n FROM commitments JOIN meetings ON meetings.id = commitments.meeting_id "
        "WHERE meetings.owner_email = ? GROUP BY commitments.owner_match", (email,),
    ).fetchall()
    db.close()
    return jsonify({"people": people, "task_owner_status": {str(r["owner_match"]): r["n"] for r in tagged}})


@app.route("/api/people/template.csv", methods=["GET"])
def people_template():
    return app.response_class(
        people_directory.TEMPLATE_CSV, mimetype="text/csv",
        headers={"Content-Disposition": "attachment; filename=people-template.csv"},
    )


@app.route("/api/people/upload", methods=["POST"])
def upload_people():
    """Body: {"csv": "<file text>", "mode": "merge" | "replace"}. merge adds new
    people and updates existing ones (matched by email); replace removes
    anyone not in the file first. Task owners are re-matched afterwards."""
    email = require_email()
    if not email:
        return jsonify({"error": "not identified"}), 401
    data = request.get_json(force=True)
    try:
        people, errors = people_directory.parse_csv(data.get("csv") or "")
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    if not people:
        return jsonify({"error": "no valid rows found", "row_errors": errors}), 400

    db = get_db()
    existing = {r["email"] for r in db.execute("SELECT email FROM people WHERE owner_email = ?", (email,)).fetchall()}
    removed = 0
    if data.get("mode") == "replace":
        keep = {p["email"] for p in people}
        removed = len(existing - keep)
        db.executemany("DELETE FROM people WHERE owner_email = ? AND email = ?", [(email, e) for e in existing - keep])
    for p in people:
        db.execute(
            "INSERT INTO people (owner_email, first_name, last_name, email, username, created_at) VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(owner_email, email) DO UPDATE SET first_name = excluded.first_name, "
            "last_name = excluded.last_name, username = excluded.username",
            (email, p["first_name"], p["last_name"], p["email"], p["username"], now_iso()),
        )
    counts = _after_directory_change(db, email)
    db.close()
    added = len({p["email"] for p in people} - existing)
    return jsonify({
        "ok": True, "added": added, "updated": len(people) - added, "removed": removed,
        "row_errors": errors, "tasks_rematched": counts,
    }), 201


@app.route("/api/people", methods=["POST"])
def add_person():
    email = require_email()
    if not email:
        return jsonify({"error": "not identified"}), 401
    person, error = _person_fields(request.get_json(force=True))
    if error:
        return jsonify({"error": error}), 400
    db = get_db()
    try:
        db.execute(
            "INSERT INTO people (owner_email, first_name, last_name, email, username, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (email, person["first_name"], person["last_name"], person["email"], person["username"], now_iso()),
        )
    except sqlite3.IntegrityError:
        db.close()
        return jsonify({"error": "someone with that email is already in the directory"}), 400
    counts = _after_directory_change(db, email)
    db.close()
    return jsonify({"ok": True, "tasks_rematched": counts}), 201


@app.route("/api/people/<int:person_id>", methods=["PATCH", "DELETE"])
def change_person(person_id):
    email = require_email()
    if not email:
        return jsonify({"error": "not identified"}), 401
    db = get_db()
    if request.method == "DELETE":
        cur = db.execute("DELETE FROM people WHERE id = ? AND owner_email = ?", (person_id, email))
    else:
        person, error = _person_fields(request.get_json(force=True))
        if error:
            db.close()
            return jsonify({"error": error}), 400
        try:
            cur = db.execute(
                "UPDATE people SET first_name = ?, last_name = ?, email = ?, username = ? WHERE id = ? AND owner_email = ?",
                (person["first_name"], person["last_name"], person["email"], person["username"], person_id, email),
            )
        except sqlite3.IntegrityError:
            db.close()
            return jsonify({"error": "someone else in the directory already has that email"}), 400
    if cur.rowcount == 0:
        db.close()
        return jsonify({"error": "not found"}), 404
    counts = _after_directory_change(db, email)
    db.close()
    return jsonify({"ok": True, "tasks_rematched": counts})


@app.route("/api/version")
def version():
    return jsonify({"version": APP_VERSION})


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


def auto_extract_commitments(db, meeting_id, meeting_date, transcript_excerpt):
    """Runs the trained ACTION classifier (transcript_parser.py) over a
    transcript and inserts whatever it finds as commitments on the given
    meeting — real extraction, not the demo-seeded fixtures below. Returns
    (count, error): error is a user-facing string when extraction couldn't
    run (most commonly the model hasn't been trained yet — see
    ml/train_action_detector.py), never raised, since a meeting should still
    get created even if extraction fails.

    Skips any utterance whose source_quote already has a commitment on this
    meeting, so calling this twice for the same transcript (the manual
    re-extract endpoint) doesn't duplicate rows — it only adds ones that are
    new since the last run."""
    if not transcript_excerpt:
        return 0, None
    # [ai-extraction v0.5.0] Accounts that chose an AI engine are handled here;
    # None means "use the built-in path below, unchanged". Remove to revert.
    if ai_extraction is not None:
        ai_items = ai_extraction.try_extract(db, meeting_id, meeting_date, transcript_excerpt)
        if ai_items is not None:
            return _insert_extracted_commitments(db, meeting_id, ai_items), None
    # [/ai-extraction v0.5.0]
    if transcript_parser is None:
        return 0, "commitment extraction isn't installed — see requirements.txt"
    try:
        extracted = transcript_parser.extract_commitments(transcript_excerpt, meeting_date=meeting_date)
    except transcript_parser.ModelNotReadyError as e:
        return 0, str(e)
    return _insert_extracted_commitments(db, meeting_id, extracted), None


def auto_extract_commitments_from_turns(db, meeting_id, meeting_date, turns):
    """Same as auto_extract_commitments, but for a source that already hands
    us turns split into {"speaker","text"} pairs (e.g. Fathom's webhook
    payload) rather than raw transcript text — skips transcript_parser's
    regex-based turn splitting entirely."""
    if not turns:
        return 0, None
    # [ai-extraction v0.5.0] See auto_extract_commitments. Remove to revert.
    if ai_extraction is not None:
        ai_items = ai_extraction.try_extract_from_turns(db, meeting_id, meeting_date, turns)
        if ai_items is not None:
            return _insert_extracted_commitments(db, meeting_id, ai_items), None
    # [/ai-extraction v0.5.0]
    if transcript_parser is None:
        return 0, "commitment extraction isn't installed — see requirements.txt"
    try:
        extracted = transcript_parser.extract_commitments_from_turns(turns, meeting_date=meeting_date)
    except transcript_parser.ModelNotReadyError as e:
        return 0, str(e)
    return _insert_extracted_commitments(db, meeting_id, extracted), None


def _insert_extracted_commitments(db, meeting_id, extracted):
    """Shared insert step: skips any utterance whose source_quote already
    has a commitment on this meeting, so calling extraction twice for the
    same transcript (the manual re-extract endpoint) doesn't duplicate rows
    — it only adds ones that are new since the last run."""
    existing_quotes = {
        row["source_quote"] for row in
        db.execute("SELECT source_quote FROM commitments WHERE meeting_id = ?", (meeting_id,)).fetchall()
    }
    people = load_people_for_meeting(db, meeting_id)

    inserted = 0
    for c in extracted:
        if c["source_quote"] in existing_quotes:
            continue
        owner_email, owner_match, _ = people_directory.match_owner(c["owner_name"], people)
        db.execute(
            "INSERT INTO commitments "
            "(meeting_id, task_text, owner_name, owner_email, due_date, status, confidence_score, source_quote, created_at, owner_match) "
            "VALUES (?, ?, ?, ?, ?, 'pending', ?, ?, ?, ?)",
            (meeting_id, c["task_text"], c["owner_name"], owner_email, c["due_date"], c["confidence_score"],
             c["source_quote"], now_iso(), owner_match),
        )
        inserted += 1
    return inserted


# ---------- people directory (owner email lookup) ----------

def load_people(db, account_email):
    return [dict(r) for r in db.execute(
        "SELECT id, first_name, last_name, email, username FROM people WHERE owner_email = ? ORDER BY last_name, first_name",
        (account_email,),
    ).fetchall()]


def load_people_for_meeting(db, meeting_id):
    row = db.execute("SELECT owner_email FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
    return load_people(db, row["owner_email"]) if row else []


def rematch_owners(db, account_email):
    """Re-runs owner lookup for this account's tasks after the directory
    changes. Never touches emails someone set by hand (owner_match 'manual')
    or emails set before 0.6.0 (owner_match NULL with an email, e.g. demo data).
    Returns {status: count} for the tasks it looked at."""
    people = load_people(db, account_email)
    rows = db.execute(
        "SELECT commitments.id, commitments.owner_name FROM commitments "
        "JOIN meetings ON meetings.id = commitments.meeting_id "
        "WHERE meetings.owner_email = ? AND (commitments.owner_match IN ('auto', 'ambiguous', 'none') "
        "OR (commitments.owner_match IS NULL AND commitments.owner_email IS NULL))",
        (account_email,),
    ).fetchall()
    counts = {"auto": 0, "ambiguous": 0, "none": 0}
    for r in rows:
        email, status, _ = people_directory.match_owner(r["owner_name"], people)
        db.execute("UPDATE commitments SET owner_email = ?, owner_match = ? WHERE id = ?", (email, status, r["id"]))
        counts[status] += 1
    return counts


def log_inbound_capture(db, user_email, provider, meeting_id, detail):
    """Shared by every real/simulated transcript-capture path: records that
    a transcript was pushed to us (as opposed to 'check now''s outbound
    direction) and links it to the meeting it created."""
    db.execute(
        "INSERT INTO transcript_sync_log (user_email, provider, direction, triggered_at, status, detail, meeting_id) "
        "VALUES (?, ?, 'inbound', ?, 'received', ?, ?)",
        (user_email, provider, now_iso(), detail, meeting_id),
    )


def looks_like_binary_text(text, sample_size=2000):
    """Heuristic guard against a binary file (.docx, .pdf, ...) getting
    submitted as transcript_excerpt — the frontend already checks the file
    extension before reading, but this catches anything that slips past it
    (drag-and-drop ignores accept=".txt" in some browsers, and this is a
    JSON API anyone could call directly). A .docx is a ZIP archive, so its
    raw bytes decoded as text start with the ZIP magic number and/or are
    full of the Unicode replacement character from invalid byte sequences."""
    sample = text[:sample_size]
    if sample.startswith("PK\x03\x04"):
        return True
    if not sample:
        return False
    return (sample.count("�") / len(sample)) > 0.01


@app.route("/api/meetings", methods=["POST"])
def create_meeting():
    """Manual transcript upload — a real write, and (when the extraction
    model is trained) commitments are auto-extracted from the transcript as
    part of the same request."""
    email = require_email()
    if not email:
        return jsonify({"error": "not identified"}), 401

    data = request.get_json(force=True)
    title = (data.get("title") or "").strip()
    meeting_date = (data.get("meeting_date") or "").strip() or today_str()
    transcript_excerpt = (data.get("transcript_excerpt") or "").strip() or None

    if not title:
        return jsonify({"error": "title is required"}), 400
    if transcript_excerpt and looks_like_binary_text(transcript_excerpt):
        return jsonify({"error": "transcript_excerpt looks like a binary file (e.g. .docx/.pdf), not plain text — convert it to .txt first"}), 400

    db = get_db()
    meeting_id = create_meeting_row(db, email, title, meeting_date, transcript_excerpt, source="upload")
    commitments_extracted, extraction_error = auto_extract_commitments(db, meeting_id, meeting_date, transcript_excerpt)
    db.commit()
    db.close()
    synced_to_tracker, tracker_sync_error = auto_sync_to_sheet(email, meeting_id)
    return jsonify({
        "id": meeting_id,
        "commitments_extracted": commitments_extracted,
        "extraction_error": extraction_error,
        "synced_to_tracker": synced_to_tracker,
        "tracker_sync_error": tracker_sync_error,
    }), 201


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


@app.route("/api/meetings/<int:meeting_id>/extract-commitments", methods=["POST"])
def extract_commitments_for_meeting(meeting_id):
    """Manually (re)runs the ACTION classifier over a meeting's saved
    transcript. Useful for a meeting that arrived before the model was
    trained, or after editing the transcript. Safe to call repeatedly —
    auto_extract_commitments() skips utterances that already have a
    commitment on this meeting, so it only adds what's new."""
    email = require_email()
    if not email:
        return jsonify({"error": "not identified"}), 401

    db = get_db()
    meeting = db.execute(
        "SELECT id, meeting_date, transcript_excerpt FROM meetings WHERE id = ? AND owner_email = ?",
        (meeting_id, email),
    ).fetchone()
    if meeting is None:
        db.close()
        return jsonify({"error": "not found"}), 404
    if not meeting["transcript_excerpt"]:
        db.close()
        return jsonify({"error": "this meeting has no saved transcript text to extract from"}), 400

    commitments_extracted, extraction_error = auto_extract_commitments(
        db, meeting_id, meeting["meeting_date"], meeting["transcript_excerpt"]
    )
    if extraction_error:
        db.close()
        return jsonify({"error": extraction_error}), 503

    db.commit()
    db.close()
    synced_to_tracker, tracker_sync_error = auto_sync_to_sheet(email, meeting_id)
    return jsonify({
        "ok": True,
        "commitments_extracted": commitments_extracted,
        "synced_to_tracker": synced_to_tracker,
        "tracker_sync_error": tracker_sync_error,
    }), 201


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

    if owner_email:
        owner_match = "manual"
    else:
        owner_email, owner_match, _ = people_directory.match_owner(owner_name, load_people(db, email))
    cur = db.execute(
        "INSERT INTO commitments "
        "(meeting_id, task_text, owner_name, owner_email, due_date, status, confidence_score, source_quote, created_at, owner_match) "
        "VALUES (?, ?, ?, ?, ?, 'pending', NULL, ?, ?, ?)",
        (meeting_id, task_text, owner_name, owner_email, due_date, source_quote, now_iso(), owner_match),
    )
    db.commit()
    db.close()
    synced_to_tracker, tracker_sync_error = auto_sync_to_sheet(email, meeting_id)
    return jsonify({"id": cur.lastrowid, "synced_to_tracker": synced_to_tracker, "tracker_sync_error": tracker_sync_error}), 201


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
    _, _, owner_candidates = people_directory.match_owner(row["owner_name"], load_people(db, email))
    db.close()

    result = with_overdue_flag(dict(row))
    result["owner_candidates"] = owner_candidates
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
    # owner_email is optional; when present it's a manual pick that
    # re-matching will never overwrite. An empty string clears it.
    set_owner = "owner_email" in data
    owner_email = (data.get("owner_email") or "").strip().lower() or None
    if status is None and not set_owner:
        return jsonify({"error": "status must be one of pending, in_progress, done"}), 400
    if status is not None and status not in ("pending", "in_progress", "done"):
        return jsonify({"error": "status must be one of pending, in_progress, done"}), 400
    if owner_email and not people_directory.EMAIL_RE.match(owner_email):
        return jsonify({"error": "that doesn't look like an email address"}), 400

    db = get_db()
    row = get_commitment_or_404(db, email, commitment_id)
    if row is None:
        db.close()
        return jsonify({"error": "not found"}), 404

    if status is not None:
        db.execute("UPDATE commitments SET status = ? WHERE id = ?", (status, commitment_id))
    if set_owner:
        db.execute(
            "UPDATE commitments SET owner_email = ?, owner_match = ? WHERE id = ?",
            (owner_email, "manual" if owner_email else None, commitment_id),
        )
    db.commit()
    db.close()
    tracker_sync_error = auto_push_status_to_sheet(email, commitment_id) if status is not None else None
    return jsonify({"ok": True, "tracker_sync_error": tracker_sync_error})


@app.route("/api/commitments/<int:commitment_id>/send-test-nudge", methods=["POST"])
def send_test_nudge(commitment_id):
    """Sends a real reminder for this task right now over one configured
    channel (0.6.0 — was a mock log entry before) and logs the outcome."""
    email = require_email()
    if not email:
        return jsonify({"error": "not identified"}), 401

    data = request.get_json(silent=True) or {}
    channel = data.get("channel")

    db = get_db()
    row = get_commitment_or_404(db, email, commitment_id)
    if row is None:
        db.close()
        return jsonify({"error": "not found"}), 404
    setting = db.execute(
        "SELECT * FROM nudge_settings WHERE user_email = ? AND channel = ?", (email, channel)
    ).fetchone()
    if setting is None:
        db.close()
        return jsonify({"error": "set up this nudge channel in Integrations first"}), 400
    db.close()

    status, detail = send_and_log(setting, dict(row), "test")
    return jsonify({"ok": status == "sent", "channel": channel, "status": status, "detail": detail}), 201


# ---------- nudges: delivery, schedule, settings ----------

NUDGE_CADENCE_REASON = {"day_before": "day_before", "on_due": "on_due", "daily_after_overdue": "overdue"}
NUDGE_REPEAT_WINDOW_HOURS = 20  # at most one attempt per task/channel/reason per ~day


def send_and_log(setting, commitment, reason):
    """Delivers one reminder and records the result in nudge_log."""
    config = json.loads(setting["config_json"] or "{}")
    status, detail = notifications.deliver(setting["channel"], setting["webhook_or_key"], config, commitment, reason)
    db = get_db()
    db.execute(
        "INSERT INTO nudge_log (commitment_id, triggered_at, channel, status, reason, detail) VALUES (?, ?, ?, ?, ?, ?)",
        (commitment["id"], now_iso(), setting["channel"], status, reason, detail),
    )
    db.commit()
    db.close()
    return status, detail


def due_nudges(db, account_email, today=None):
    """Works out which (channel setting, task, reason) reminders are due now
    for one account, per each enabled channel's cadence. Skips tasks that are
    done, and anything already attempted recently (or, for the one-off
    'day before' / 'on the due date' reminders, already sent)."""
    today = today or date.today()
    settings = db.execute(
        "SELECT * FROM nudge_settings WHERE user_email = ? AND enabled = 1", (account_email,)
    ).fetchall()
    tasks = db.execute(
        "SELECT commitments.*, meetings.title AS meeting_title FROM commitments "
        "JOIN meetings ON meetings.id = commitments.meeting_id "
        "WHERE meetings.owner_email = ? AND commitments.status != 'done' AND commitments.due_date IS NOT NULL",
        (account_email,),
    ).fetchall()
    cutoff = (datetime.utcnow() - timedelta(hours=NUDGE_REPEAT_WINDOW_HOURS)).isoformat(timespec="seconds")

    plan = []
    for setting in settings:
        reason = NUDGE_CADENCE_REASON.get(setting["cadence"])
        for task in tasks:
            try:
                due = date.fromisoformat(task["due_date"])
            except ValueError:
                continue
            if not ((reason == "day_before" and due == today + timedelta(days=1))
                    or (reason == "on_due" and due == today)
                    or (reason == "overdue" and due < today)):
                continue
            recent = db.execute(
                "SELECT 1 FROM nudge_log WHERE commitment_id = ? AND channel = ? AND reason = ? "
                "AND (triggered_at >= ? OR (status = 'sent' AND ? != 'overdue')) LIMIT 1",
                (task["id"], setting["channel"], reason, cutoff, reason),
            ).fetchone()
            if not recent:
                plan.append((setting, dict(task), reason))
    return plan


def run_nudges(account_email, today=None):
    """Sends every reminder that's due for one account. Returns a result list."""
    db = get_db()
    plan = due_nudges(db, account_email, today)
    db.close()
    results = []
    for setting, task, reason in plan:
        status, detail = send_and_log(setting, task, reason)
        results.append({"commitment_id": task["id"], "task": task["task_text"], "channel": setting["channel"],
                        "reason": reason, "status": status, "detail": detail})
    return results


def nudge_scheduler_loop(interval_seconds):
    """Background thread: every interval, runs due nudges for each account
    that switched on automatic nudges. Never dies on an error."""
    import time
    while True:
        time.sleep(interval_seconds)
        try:
            db = get_db()
            accounts = [r["user_email"] for r in db.execute(
                "SELECT user_email FROM nudge_preferences WHERE auto_enabled = 1").fetchall()]
            db.close()
            for account in accounts:
                run_nudges(account)
        except Exception as e:
            print(f"[nudges] scheduler pass failed: {e}")


def start_nudge_scheduler():
    import threading
    interval = int(os.environ.get("NUDGE_CHECK_INTERVAL_SECONDS", "900"))
    threading.Thread(target=nudge_scheduler_loop, args=(interval,), daemon=True, name="nudge-scheduler").start()
    print(f"[nudges] automatic nudge checks every {interval}s (for accounts that switched them on)")


def _mask(value):
    value = value or ""
    return ("••••" + value[-4:]) if len(value) > 4 else ("••••" if value else "")


@app.route("/api/nudge-settings", methods=["GET"])
def list_nudge_settings():
    """Secrets (webhook URLs, tokens, SMTP password) are masked — the full
    values never go back to the browser."""
    email = require_email()
    if not email:
        return jsonify({"error": "not identified"}), 401
    db = get_db()
    rows = db.execute(
        "SELECT * FROM nudge_settings WHERE user_email = ? ORDER BY channel", (email,)
    ).fetchall()
    db.close()
    result = []
    for r in rows:
        item = dict(r)
        config = json.loads(item.pop("config_json") or "{}")
        item["has_password"] = bool(config.pop("password", ""))
        item["config"] = config
        if r["channel"] != "email":
            item["webhook_or_key"] = _mask(r["webhook_or_key"])
        result.append(item)
    return jsonify(result)


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
    incoming = data.get("config") or {}

    if channel not in ("slack", "teams", "email", "custom"):
        return jsonify({"error": "channel must be one of slack, teams, email, custom"}), 400
    if channel == "custom" and not custom_name:
        return jsonify({"error": "custom_name is required for a custom channel"}), 400
    if cadence not in ("day_before", "on_due", "daily_after_overdue"):
        return jsonify({"error": "invalid cadence"}), 400

    db = get_db()
    existing = db.execute(
        "SELECT webhook_or_key, config_json FROM nudge_settings WHERE user_email = ? AND channel = ?", (email, channel)
    ).fetchone()
    previous = json.loads(existing["config_json"] or "{}") if existing else {}

    config = {}
    if channel == "email":
        config = {
            "smtp_host": (incoming.get("smtp_host") or "").strip(),
            "smtp_port": str(incoming.get("smtp_port") or "").strip(),
            "smtp_security": incoming.get("smtp_security") if incoming.get("smtp_security") in ("starttls", "ssl", "none") else "starttls",
            "username": (incoming.get("username") or "").strip(),
            # A blank password field keeps the saved one (it's never sent to the browser).
            "password": incoming.get("password") or previous.get("password", ""),
            "from_email": (incoming.get("from_email") or "").strip(),
        }
        missing = [f for f in notifications.EMAIL_REQUIRED if not config[f]]
        if missing:
            db.close()
            return jsonify({"error": f"missing email setting(s): {', '.join(missing)}"}), 400
        if not config["smtp_port"].isdigit():
            db.close()
            return jsonify({"error": "SMTP port must be a number, e.g. 587"}), 400
        webhook_or_key = config["from_email"]
    else:
        # A blank field keeps the saved URL/token (the browser only sees it masked).
        webhook_or_key = webhook_or_key or (existing["webhook_or_key"] if existing else "")
        if not webhook_or_key:
            db.close()
            return jsonify({"error": "webhook_or_key is required"}), 400

    db.execute(
        "INSERT INTO nudge_settings (user_email, channel, custom_name, webhook_or_key, cadence, enabled, config_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(user_email, channel) DO UPDATE SET "
        "custom_name = excluded.custom_name, webhook_or_key = excluded.webhook_or_key, "
        "cadence = excluded.cadence, enabled = excluded.enabled, config_json = excluded.config_json",
        (email, channel, custom_name, webhook_or_key, cadence, enabled, json.dumps(config)),
    )
    db.commit()
    db.close()
    return jsonify({"ok": True}), 201


@app.route("/api/nudge-settings/<channel>", methods=["DELETE"])
def delete_nudge_setting(channel):
    email = require_email()
    if not email:
        return jsonify({"error": "not identified"}), 401
    db = get_db()
    db.execute("DELETE FROM nudge_settings WHERE user_email = ? AND channel = ?", (email, channel))
    db.commit()
    db.close()
    return jsonify({"ok": True})


@app.route("/api/nudge-settings/<channel>/test", methods=["POST"])
def test_nudge_channel(channel):
    """Sends a sample reminder addressed to the signed-in account's own
    email, to check a channel works before any real task depends on it."""
    email = require_email()
    if not email:
        return jsonify({"error": "not identified"}), 401
    db = get_db()
    setting = db.execute(
        "SELECT * FROM nudge_settings WHERE user_email = ? AND channel = ?", (email, channel)
    ).fetchone()
    db.close()
    if setting is None:
        return jsonify({"error": "save this channel first"}), 400
    sample = {"id": None, "task_text": "Sample task — this is a test of your nudge settings", "owner_name": "you",
              "owner_email": email, "due_date": date.today().isoformat(), "status": "pending",
              "meeting_title": "Commitment Tracker test"}
    status, detail = notifications.deliver(setting["channel"], setting["webhook_or_key"],
                                           json.loads(setting["config_json"] or "{}"), sample, "test")
    return jsonify({"ok": status == "sent", "status": status, "detail": detail}), (200 if status == "sent" else 502)


@app.route("/api/nudge-preferences", methods=["GET", "POST"])
def nudge_preferences():
    email = require_email()
    if not email:
        return jsonify({"error": "not identified"}), 401
    db = get_db()
    if request.method == "POST":
        auto = 1 if (request.get_json(force=True) or {}).get("auto_enabled") else 0
        db.execute(
            "INSERT INTO nudge_preferences (user_email, auto_enabled) VALUES (?, ?) "
            "ON CONFLICT(user_email) DO UPDATE SET auto_enabled = excluded.auto_enabled",
            (email, auto),
        )
        db.commit()
    row = db.execute("SELECT auto_enabled FROM nudge_preferences WHERE user_email = ?", (email,)).fetchone()
    db.close()
    return jsonify({"auto_enabled": bool(row and row["auto_enabled"])})


@app.route("/api/nudges/run-now", methods=["POST"])
def run_nudges_now():
    """Sends whatever reminders are due right now (per each channel's
    cadence), whether or not automatic nudges are switched on."""
    email = require_email()
    if not email:
        return jsonify({"error": "not identified"}), 401
    results = run_nudges(email)
    return jsonify({"ok": True, "results": results})


@app.route("/api/nudges/log", methods=["GET"])
def nudge_activity():
    email = require_email()
    if not email:
        return jsonify({"error": "not identified"}), 401
    db = get_db()
    rows = db.execute(
        "SELECT nudge_log.*, commitments.task_text, commitments.owner_name FROM nudge_log "
        "JOIN commitments ON commitments.id = nudge_log.commitment_id "
        "JOIN meetings ON meetings.id = commitments.meeting_id "
        "WHERE meetings.owner_email = ? ORDER BY nudge_log.id DESC LIMIT 20",
        (email,),
    ).fetchall()
    db.close()
    return jsonify([dict(r) for r in rows])


# ---------- external task tracker (Jira / Google Sheets) ----------

TRACKER_REQUIRED_FIELDS = {
    "jira": ["base_url", "project_key", "email", "api_token"],
    # No api_key here: auth is the local OAuth token from google_oauth_setup.py
    # (see google_sheets.py), not a per-user credential entered in this form.
    "google_sheets": ["spreadsheet_id"],
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
    """Jira and Custom stay mocked (logs what would be sent, no real call).
    Google Sheets is real: the first sync writes an actual row via the
    Sheets API (google_sheets.py) and mints external_ref from the real
    written row; a later sync for the same commitment rewrites that row's
    status cell instead of adding a duplicate row."""
    email = require_email()
    if not email:
        return jsonify({"error": "not identified"}), 401

    db = get_db()
    # Take the write lock before reading external_ref: without it, a double
    # click sends two requests that both see "not synced yet" and both write
    # a row to the sheet. The second request now waits here until the first
    # commits, then sees its external_ref.
    db.execute("BEGIN IMMEDIATE")
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
    external_ref = row["external_ref"]

    if provider == "google_sheets":
        if google_sheets is None:
            db.close()
            return jsonify({"error": "Google Sheets integration isn't installed — see requirements.txt"}), 503
        try:
            if external_ref:
                google_sheets.update_status(config.get("spreadsheet_id"), external_ref, row["status"])
                detail = f"Updated status of {external_ref} to '{row['status']}' in Google Sheet {config.get('spreadsheet_id')}"
            else:
                external_ref = google_sheets.append_commitment_row(config.get("spreadsheet_id"), dict(row))
                db.execute("UPDATE commitments SET external_ref = ? WHERE id = ?", (external_ref, commitment_id))
                detail = f"Wrote {external_ref} to Google Sheet {config.get('spreadsheet_id')}"
        except google_sheets.GoogleAuthNotReadyError as e:
            db.close()
            return jsonify({"error": str(e)}), 503
        except Exception as e:
            db.close()
            return jsonify({"error": f"Google Sheets API call failed: {e}"}), 502
    else:
        # Mocked: mints a fake external reference so the inbound side still
        # has something to match a later status update against.
        if not external_ref:
            if provider == "jira":
                external_ref = f"{config.get('project_key', 'TASK')}-{commitment_id}"
            else:
                external_ref = f"ext-{commitment_id}"
            db.execute("UPDATE commitments SET external_ref = ? WHERE id = ?", (external_ref, commitment_id))

        if provider == "jira":
            detail = f"Would create/update Jira issue {external_ref} in project {config.get('project_key')} at {config.get('base_url')}"
        else:
            detail = f"Would send {external_ref} to {tracker['custom_name'] or 'your custom tracker'} at {config.get('endpoint')}"

    sync_status = "synced" if provider == "google_sheets" else "mock_synced"
    triggered_at = now_iso()
    db.execute(
        "INSERT INTO tracker_sync_log (commitment_id, provider, direction, triggered_at, status, detail) "
        "VALUES (?, ?, 'outbound', ?, ?, ?)",
        (commitment_id, provider, triggered_at, sync_status, detail),
    )
    db.commit()
    db.close()
    return jsonify({
        "ok": True, "triggered_at": triggered_at, "provider": provider,
        "status": sync_status, "detail": detail, "external_ref": external_ref,
    }), 201


def get_sheets_tracker_config(db, email):
    """The user's Google Sheets tracker config, or None if Sheets isn't the
    connected, enabled tracker."""
    tracker = db.execute(
        "SELECT provider, config_json, enabled FROM tracker_settings WHERE user_email = ?", (email,)
    ).fetchone()
    if tracker is None or tracker["provider"] != "google_sheets" or not tracker["enabled"]:
        return None
    return json.loads(tracker["config_json"])


def push_unsynced_to_sheet(db, email, config, meeting_id=None):
    """Writes every not-yet-synced commitment of this user (optionally just
    one meeting's) to the sheet in a single API call. The caller must already
    hold the write lock (BEGIN IMMEDIATE) so two concurrent runs can't both
    write the same commitments. Returns (count_written, error_or_None)."""
    query = (
        "SELECT commitments.* FROM commitments JOIN meetings ON meetings.id = commitments.meeting_id "
        "WHERE meetings.owner_email = ? AND commitments.external_ref IS NULL"
    )
    params = [email]
    if meeting_id is not None:
        query += " AND commitments.meeting_id = ?"
        params.append(meeting_id)
    rows = db.execute(query + " ORDER BY commitments.id", params).fetchall()
    if not rows:
        return 0, None
    if google_sheets is None:
        return 0, "Google Sheets integration isn't installed — see requirements.txt"

    spreadsheet_id = config.get("spreadsheet_id")
    try:
        refs = google_sheets.append_commitment_rows(spreadsheet_id, [dict(r) for r in rows])
    except google_sheets.GoogleAuthNotReadyError as e:
        return 0, str(e)
    except Exception as e:
        return 0, f"Google Sheets API call failed: {e}"

    triggered_at = now_iso()
    for r, ref in zip(rows, refs):
        db.execute("UPDATE commitments SET external_ref = ? WHERE id = ?", (ref, r["id"]))
        db.execute(
            "INSERT INTO tracker_sync_log (commitment_id, provider, direction, triggered_at, status, detail) "
            "VALUES (?, 'google_sheets', 'outbound', ?, 'synced', ?)",
            (r["id"], triggered_at, f"Wrote {ref} to Google Sheet {spreadsheet_id}"),
        )
    return len(rows), None


def auto_sync_to_sheet(email, meeting_id=None):
    """Runs after a request has committed its own writes (new meeting,
    extracted or hand-added commitments): if the user's Google Sheets tracker
    has auto-sync on, pushes whatever isn't in the sheet yet. Never raises —
    a sync failure mustn't undo or fail the upload that triggered it; the
    error is returned for the caller to surface. Returns (count, error)."""
    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    config = get_sheets_tracker_config(db, email)
    if not config or not config.get("auto_sync"):
        db.close()
        return 0, None
    count, error = push_unsynced_to_sheet(db, email, config, meeting_id)
    db.commit()
    db.close()
    return count, error


def auto_push_status_to_sheet(email, commitment_id):
    """Mirrors an in-app status change onto the commitment's sheet row, if
    it's already synced and auto-sync is on. Returns an error string or None."""
    db = get_db()
    config = get_sheets_tracker_config(db, email)
    row = db.execute("SELECT status, external_ref FROM commitments WHERE id = ?", (commitment_id,)).fetchone()
    if not config or not config.get("auto_sync") or row is None or not row["external_ref"] or google_sheets is None:
        db.close()
        return None
    try:
        google_sheets.update_status(config.get("spreadsheet_id"), row["external_ref"], row["status"])
    except Exception as e:
        db.close()
        return f"Couldn't update the Google Sheet: {e}"
    db.execute(
        "INSERT INTO tracker_sync_log (commitment_id, provider, direction, triggered_at, status, detail) "
        "VALUES (?, 'google_sheets', 'outbound', ?, 'synced', ?)",
        (commitment_id, now_iso(), f"Updated status of {row['external_ref']} to '{row['status']}'"),
    )
    db.commit()
    db.close()
    return None


@app.route("/api/tracker/sync-all", methods=["POST"])
def sync_all_to_tracker():
    """Pushes every not-yet-synced commitment to the Google Sheet in one call
    — the bulk alternative to clicking "Sync to tracker" per commitment.
    Works whether or not auto-sync is on (it's how you catch up the backlog
    that existed before auto-sync was enabled)."""
    email = require_email()
    if not email:
        return jsonify({"error": "not identified"}), 401

    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    config = get_sheets_tracker_config(db, email)
    if not config:
        db.close()
        return jsonify({"error": "bulk sync needs Google Sheets connected as your task tracker (Integrations)"}), 400
    count, error = push_unsynced_to_sheet(db, email, config)
    if error:
        db.close()
        return jsonify({"error": error}), 502
    db.commit()
    db.close()
    return jsonify({"ok": True, "synced": count}), 201


TRACKER_STATUS_SYNONYMS = {
    "pending": "pending", "open": "pending", "to_do": "pending", "todo": "pending",
    "not_started": "pending", "new": "pending", "backlog": "pending",
    "in_progress": "in_progress", "inprogress": "in_progress", "started": "in_progress",
    "doing": "in_progress", "wip": "in_progress", "active": "in_progress",
    "done": "done", "closed": "done", "complete": "done", "completed": "done",
    "resolved": "done", "finished": "done",
}


def normalize_tracker_status(value):
    """Maps whatever a person typed into the tracker ("In Progress", "Closed",
    "To do") onto this app's three statuses, or None if unrecognized."""
    key = re.sub(r"[\s\-]+", "_", str(value or "").strip().lower())
    return TRACKER_STATUS_SYNONYMS.get(key)


def apply_inbound_tracker_update(db, commitment_id, new_status, provider, source_label):
    """Shared by the real webhook and the UI's simulate button: applies a
    status change that originated in the external tracker, and logs it as
    an inbound sync event (as opposed to the outbound 'push' direction)."""
    new_status = normalize_tracker_status(new_status)
    if new_status is None:
        return None, "status must be one of pending, in_progress, done (or a synonym like Open, In progress, Closed)"

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
    # webhook_secret is the "whsec_..." value Fathom hands back when you
    # create the webhook — used to verify its signature in the real
    # /api/webhooks/fathom/<token> endpoint below.
    "fathom": ["webhook_secret"],
    "custom": ["account_identifier", "api_key"],
}
TRANSCRIPT_SOURCE_LABELS = {
    "zoom": "Zoom", "google_meet": "Google Meet", "teams": "Microsoft Teams", "otter": "Otter.ai",
    "fathom": "Fathom",
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

    if provider not in ("none", "zoom", "google_meet", "teams", "otter", "fathom", "custom"):
        return jsonify({"error": "provider must be one of none, zoom, google_meet, teams, otter, fathom, custom"}), 400
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
    if transcript_excerpt and looks_like_binary_text(transcript_excerpt):
        db.close()
        return jsonify({"error": "transcript_excerpt looks like a binary file, not plain text"}), 400

    meeting_id = create_meeting_row(db, source["user_email"], title, meeting_date, transcript_excerpt, source="live", transcript_provider=provider)
    log_inbound_capture(db, source["user_email"], provider, meeting_id, f'Received live transcript for "{title}"')
    commitments_extracted, extraction_error = auto_extract_commitments(db, meeting_id, meeting_date, transcript_excerpt)
    db.commit()
    db.close()
    synced_to_tracker, tracker_sync_error = auto_sync_to_sheet(source["user_email"], meeting_id)
    return jsonify({
        "ok": True,
        "meeting_id": meeting_id,
        "commitments_extracted": commitments_extracted,
        "extraction_error": extraction_error,
        "synced_to_tracker": synced_to_tracker,
        "tracker_sync_error": tracker_sync_error,
    }), 201


def verify_fathom_signature(secret, req):
    """Fathom's webhook signatures follow the Svix convention (matches its
    documented webhook-id / webhook-timestamp / webhook-signature headers):
    HMAC-SHA256 over "<id>.<timestamp>.<raw body>", base64-encoded, checked
    against the space-delimited "v1,<sig>" entries in webhook-signature. The
    stored secret is the "whsec_<base64>" value Fathom hands back when the
    webhook is created."""
    webhook_id = req.headers.get("webhook-id")
    webhook_timestamp = req.headers.get("webhook-timestamp")
    signature_header = req.headers.get("webhook-signature")
    if not (secret and webhook_id and webhook_timestamp and signature_header):
        return False

    secret_bytes = base64.b64decode(secret.split("_", 1)[1]) if secret.startswith("whsec_") else secret.encode()
    signed_content = f"{webhook_id}.{webhook_timestamp}.{req.get_data(as_text=True)}"
    expected = base64.b64encode(hmac.new(secret_bytes, signed_content.encode(), hashlib.sha256).digest()).decode()

    for part in signature_header.split():
        sig = part.split(",", 1)[1] if "," in part else part
        if hmac.compare_digest(sig, expected):
            return True
    return False


def _fathom_speaker_turns(payload):
    """Fathom's transcript is already split into speaker turns — pull out
    (speaker, text) pairs defensively, since the exact envelope (fields at
    the top level vs nested under "meeting"/"data") isn't pinned down by
    Fathom's public docs. Falls back to an empty turn list rather than
    raising, so a shape mismatch shows up as '0 commitments extracted' in
    the Integrations activity log instead of a 500 — check that log's detail
    text (the raw payload keys) if this ever comes back empty for a meeting
    that clearly had content."""
    meeting = payload
    for key in ("meeting", "data"):
        if isinstance(payload.get(key), dict):
            meeting = payload[key]
            break
    turns = []
    for entry in meeting.get("transcript") or []:
        speaker = entry.get("speaker") or {}
        name = speaker.get("display_name") or speaker.get("name") or "UNKNOWN"
        text = (entry.get("text") or "").strip()
        if text:
            turns.append({"speaker": name, "text": text})
    return meeting, turns


@app.route("/api/webhooks/fathom/<token>", methods=["POST"])
def fathom_webhook(token):
    """Real, functional inbound endpoint for Fathom's "new meeting content
    ready" webhook (developers.fathom.ai/webhooks) — configure it in Fathom
    with include_transcript=true, pointed at this URL. Fathom hands the
    transcript to us already split into speaker turns, so this goes through
    auto_extract_commitments_from_turns rather than re-deriving turns from
    raw text.

    Not session-protected — Fathom has no browser session — so the user is
    identified via the webhook_token in the URL, and the payload's
    authenticity is checked via Fathom's HMAC signature headers instead of a
    session cookie."""
    db = get_db()
    source = db.execute(
        "SELECT user_email, provider, config_json FROM transcript_source_settings WHERE webhook_token = ?", (token,)
    ).fetchone()
    if source is None:
        db.close()
        return jsonify({"error": "unrecognized webhook token"}), 404
    if source["provider"] != "fathom":
        db.close()
        return jsonify({"error": "this transcript source isn't configured for Fathom"}), 400

    config = json.loads(source["config_json"])
    if not verify_fathom_signature(config.get("webhook_secret", ""), request):
        db.close()
        return jsonify({"error": "signature verification failed"}), 401

    payload = request.get_json(force=True)
    meeting, turns = _fathom_speaker_turns(payload)
    title = meeting.get("title") or meeting.get("meeting_title") or "Fathom meeting"
    raw_date = meeting.get("scheduled_start_time") or meeting.get("recording_start_time") or meeting.get("created_at")
    meeting_date = (raw_date or "")[:10] or today_str()
    transcript_excerpt = "\n\n".join(f"{t['speaker']}: {t['text']}" for t in turns) or None

    meeting_id = create_meeting_row(db, source["user_email"], title, meeting_date, transcript_excerpt, source="live", transcript_provider="fathom")
    log_inbound_capture(db, source["user_email"], "fathom", meeting_id, f'Received Fathom transcript for "{title}" ({len(turns)} turns)')
    commitments_extracted, extraction_error = auto_extract_commitments_from_turns(db, meeting_id, meeting_date, turns)
    db.commit()
    db.close()
    synced_to_tracker, tracker_sync_error = auto_sync_to_sheet(source["user_email"], meeting_id)
    return jsonify({
        "ok": True,
        "meeting_id": meeting_id,
        "commitments_extracted": commitments_extracted,
        "extraction_error": extraction_error,
        "synced_to_tracker": synced_to_tracker,
        "tracker_sync_error": tracker_sync_error,
    }), 201


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
    commitments_extracted, extraction_error = auto_extract_commitments(db, meeting_id, meeting_date, transcript_excerpt)
    db.commit()
    db.close()
    synced_to_tracker, tracker_sync_error = auto_sync_to_sheet(email, meeting_id)
    return jsonify({
        "ok": True,
        "meeting_id": meeting_id,
        "commitments_extracted": commitments_extracted,
        "extraction_error": extraction_error,
        "synced_to_tracker": synced_to_tracker,
        "tracker_sync_error": tracker_sync_error,
    }), 201


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


# ---------- [ai-extraction v0.5.0] optional feature — remove this block to revert ----------
# Lets an account extract commitments with its own Claude / OpenAI-compatible
# API key instead of the built-in model. All logic and routes live in
# ai_extraction.py. AI_EXTRACTION_ENABLED=0 turns it off: the page and routes
# aren't registered and extraction behaves exactly as in v0.4.0.
ai_extraction = None
if os.environ.get("AI_EXTRACTION_ENABLED", "1") != "0":
    try:
        import ai_extraction
        ai_extraction.init_app(app, get_db, require_email, transcript_parser)
    except ImportError:
        ai_extraction = None
# ---------- [/ai-extraction v0.5.0] ----------


if __name__ == "__main__":
    if not DB_PATH.exists():
        init_db()
        print(f"Initialized new database at {DB_PATH}")
    migrate_db()
    # With the debug reloader, this block runs in a watcher process and in the
    # process that actually serves requests; only start the scheduler in the latter.
    if os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        start_nudge_scheduler()
    app.run(debug=True, port=5000)
