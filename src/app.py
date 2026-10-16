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

Authentication is real (0.9.0, auth.py): email + password (hashed, with
lockout after repeated failures) or single sign-on through any OpenID Connect
provider (Google, Microsoft, Okta...). The signed-in email lives in a signed,
HttpOnly session cookie; require_email() checks it against the account's
session_version on every request. State-changing API calls need the
X-CT-Request header (CSRF guard).

Slow or failure-prone work runs as background jobs (0.9.0, jobs.py):
commitment extraction after an upload or webhook, retries of a failed Google
Sheets sync, and the automatic nudge check. Uploads and webhooks return
straight away; the dashboard updates when the job finishes.

Inbound webhooks are signed (0.9.0, webhook_security.py): HMAC-SHA256 with a
per-account signing secret, a 5-minute timestamp window, replay detection and
rate limits. Fathom keeps its own signature scheme plus the same replay checks.

Uploads accept .txt, .docx and .pdf (0.9.0, document_text.py). Email nudges
are written from the account's own draft and, optionally, tailored by AI to
each recipient's designation (0.9.0, ai_writer.py).

Nudges are real (0.6.0): notifications.py delivers them by email (SMTP),
Slack, Teams or a custom webhook, on demand or on each channel's cadence via
the in-process scheduler thread, and every attempt is logged in `nudge_log`.

The external task tracker (`tracker_settings`, one per user): Google Sheets
is real (google_sheets.py, 0.3.0+); Jira and Custom are still mocked — "sync
to tracker" only logs what *would* be sent to `tracker_sync_log`. Users who
don't configure a tracker are unaffected.

Stored credentials (API keys, tokens, SMTP password, webhook secrets) are
encrypted at rest (0.8.0, secret_store.py) and never returned to the
browser in full.

Live updates (0.8.0): GET /api/events is a Server-Sent Events stream per
account. Routes call notify() after committing a change, and open
dashboards re-fetch immediately instead of polling — see live_events.py.

Tracker sync is two-way in shape, not just push: outbound ("sync to
tracker") is mocked for Jira/Custom, but `/api/webhooks/tracker-update` is a *real*,
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

Run (from the project root — all code is in src/, data stays in the root):
    pip install -r requirements.txt
    python -m spacy download en_core_web_sm
    python src/ml/train_action_detector.py   # one-time: trains ml/action_detector.keras
    python src/app.py                        # CT_DEBUG=1 for the auto-reloading debug server
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
import queue
import re
import secrets
import sqlite3
import urllib.error
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from flask import Flask, Response, g, has_request_context, jsonify, redirect, request, send_from_directory

import ai_writer
import auth
import document_text
import jobs
import live_events
import notifications
import people_directory
import secret_store
import webhook_security

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
APP_VERSION = "0.10.3"

# Code lives in src/ (this folder); runtime data — the database, secret.key,
# Google tokens, ML models — stays in the project root one level up.
SRC_DIR = Path(__file__).resolve().parent
BASE_DIR = SRC_DIR.parent
DB_PATH = BASE_DIR / "commitment_tracker.db"
SCHEMA_PATH = SRC_DIR / "schema.sql"

app = Flask(__name__, static_folder="static", static_url_path="")
# Uploads: a 20 MB transcript file plus form fields; anything bigger is refused with 413.
app.config["MAX_CONTENT_LENGTH"] = document_text.MAX_UPLOAD_BYTES + 1024 * 1024


def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


# Session settings, sign-in routes and the CSRF guard (secret key from
# flask_secret.key / COMMITMENT_TRACKER_FLASK_SECRET — no longer in source).
auth.init_app(app, get_db)


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        conn.row_factory = sqlite3.Row
        auth.migrate(conn)
        jobs.ensure_schema(conn)
        webhook_security.ensure_schema(conn)


def migrate_db():
    """Adds columns introduced after a user's local DB was first created.
    SQLite has no 'ADD COLUMN IF NOT EXISTS', so check pragma table_info first."""
    if not DB_PATH.exists():
        return
    with sqlite3.connect(DB_PATH) as conn:
        # Every statement in schema.sql is CREATE ... IF NOT EXISTS, so re-running
        # it adds tables introduced after this DB was created (e.g. people).
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        # 0.9.0: password / SSO columns and tables, job queue, webhook security
        auth.migrate(conn)
        jobs.ensure_schema(conn)
        webhook_security.ensure_schema(conn)
        # 0.10.0: when a task last changed, for "most recent edit wins" with the sheet
        if "updated_at" not in {row[1] for row in conn.execute("PRAGMA table_info(commitments)")}:
            conn.execute("ALTER TABLE commitments ADD COLUMN updated_at TEXT")
        people_cols = {row[1] for row in conn.execute("PRAGMA table_info(people)")}
        if "designation" not in people_cols:
            conn.execute("ALTER TABLE people ADD COLUMN designation TEXT NOT NULL DEFAULT ''")

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
    encrypt_stored_secrets()


# Columns holding credentials, encrypted at rest since 0.8.0 (secret_store.py).
# ai_extraction_settings is created lazily, so it may not exist yet.
SECRET_COLUMNS = [
    ("nudge_settings", "webhook_or_key"),
    ("nudge_settings", "config_json"),
    ("tracker_settings", "config_json"),
    ("transcript_source_settings", "config_json"),
    ("ai_extraction_settings", "config_json"),
]
PRE_ENCRYPTION_BACKUP = BASE_DIR / "_snapshots" / "v0.7.3" / "commitment_tracker.db.bak"


def encrypt_stored_secrets():
    """Re-writes any plain-text credential left by a pre-0.8.0 DB in
    encrypted form. Idempotent. The first time it finds something to
    encrypt it copies the DB to PRE_ENCRYPTION_BACKUP — if the key were
    lost, that copy is the only way back to the credentials. Returns the
    number of values encrypted."""
    with sqlite3.connect(DB_PATH) as conn:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
        pending = []
        for table, column in SECRET_COLUMNS:
            if table not in tables:
                continue
            for rowid, value in conn.execute(f"SELECT rowid, {column} FROM {table}"):
                if value and not secret_store.is_encrypted(value):
                    pending.append((table, column, rowid, value))
        if not pending:
            return 0
        if DB_PATH == BASE_DIR / "commitment_tracker.db" and not PRE_ENCRYPTION_BACKUP.exists():
            import shutil
            PRE_ENCRYPTION_BACKUP.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(DB_PATH, PRE_ENCRYPTION_BACKUP)
            print(f"[secrets] backed up the database to {PRE_ENCRYPTION_BACKUP} before encrypting saved credentials")
        for table, column, rowid, value in pending:
            conn.execute(f"UPDATE {table} SET {column} = ? WHERE rowid = ?", (secret_store.encrypt(value), rowid))
        conn.commit()
    print(f"[secrets] encrypted {len(pending)} saved credential value(s)")
    return len(pending)


def now_iso():
    return datetime.utcnow().isoformat(timespec="seconds")


def today_str():
    return date.today().isoformat()


def require_email():
    """The signed-in account's email, or None (caller returns 401). Checked
    against the database each time, so a password change or "sign out
    everywhere" ends other sessions immediately (auth.session_email)."""
    if has_request_context() and "account_email" in g:
        return g.account_email
    db = get_db()
    try:
        email = auth.session_email(db)
    finally:
        db.close()
    g.account_email = email
    return email


# ---------- live updates (Server-Sent Events) ----------

SSE_HEARTBEAT_SECONDS = 15


def notify(account_email, kind, **data):
    """Tells the account's open dashboards that something changed. Inside a
    request the event is held until the response succeeds (by then every
    write is committed, so a browser that re-fetches sees it); outside one
    (the nudge scheduler thread) it goes out immediately."""
    if has_request_context():
        g.setdefault("pending_events", []).append((account_email, kind, data))
    else:
        live_events.publish(account_email, kind, data)


@app.errorhandler(secret_store.SecretUnreadable)
def secret_unreadable(e):
    """A saved credential was encrypted with a different key — tell the user
    to re-enter it rather than failing with a bare 500."""
    return jsonify({"error": str(e)}), 409


@app.after_request
def flush_live_events(response):
    for account_email, kind, data in g.pop("pending_events", []):
        if response.status_code < 400:
            live_events.publish(account_email, kind, data)
    return response


@app.after_request
def security_and_cache_headers(response):
    # no-cache = the browser must re-check with the server before reusing a
    # page or API answer, so a settings page never shows stale saved values
    # and a code update is picked up on the next load.
    response.headers.setdefault("Cache-Control", "no-cache")
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "same-origin")
    return response


@app.errorhandler(413)
def too_large(e):
    return jsonify({"error": f"the upload is too large — the limit is {document_text.MAX_UPLOAD_BYTES // (1024 * 1024)} MB"}), 413


@app.before_request
def ensure_background_services():
    if not app.testing:
        start_background_services()


@app.route("/api/events")
def live_event_stream():
    """Server-Sent Events stream for the signed-in account. Each message is
    `event: <kind>` + a JSON `data:` line; a comment ping goes out every
    SSE_HEARTBEAT_SECONDS so dead connections are noticed (and ngrok
    doesn't close an idle one)."""
    email = require_email()
    if not email:
        return jsonify({"error": "not identified"}), 401

    def stream():
        q = live_events.subscribe(email)
        try:
            yield "retry: 3000\nevent: ready\ndata: {}\n\n"
            while True:
                try:
                    event = q.get(timeout=SSE_HEARTBEAT_SECONDS)
                except queue.Empty:
                    yield ": ping\n\n"
                    continue
                yield f"event: {event['kind']}\ndata: {json.dumps(event['data'])}\n\n"
        finally:
            live_events.unsubscribe(email, q)

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})


# ---------- static frontend ----------

@app.route("/")
def index():
    return send_from_directory(app.static_folder, "index.html")


def signed_in_page(filename):
    """Serves an app page, or sends a signed-out visitor to the sign-in page."""
    if not require_email():
        return redirect("/")
    return send_from_directory(app.static_folder, filename)


@app.route("/dashboard")
def dashboard_page():
    return signed_in_page("dashboard.html")


@app.route("/settings")
def settings_page():
    return signed_in_page("settings.html")


@app.route("/people")
def people_page():
    return signed_in_page("people.html")


@app.route("/jobs")
def jobs_page():
    return signed_in_page("jobs.html")


@app.route("/account")
def account_page():
    return signed_in_page("account.html")


# ---------- people directory API ----------

def _person_fields(data):
    person = {
        "first_name": (data.get("first_name") or "").strip(),
        "last_name": (data.get("last_name") or "").strip(),
        "email": (data.get("email") or "").strip().lower(),
        "username": (data.get("username") or "").strip(),
        "designation": (data.get("designation") or "").strip()[:120],
    }
    if not people_directory.EMAIL_RE.match(person["email"]):
        return None, "a valid email is required"
    if not (person["first_name"] or person["last_name"] or person["username"]):
        return None, "add at least a first name, last name or user name"
    return person, None


def _after_directory_change(db, email):
    counts = rematch_owners(db, email)
    db.commit()
    notify(email, "commitments_changed", reason="people_directory")
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
        # A file without a Designation column keeps the designations already saved.
        db.execute(
            "INSERT INTO people (owner_email, first_name, last_name, email, username, designation, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(owner_email, email) DO UPDATE SET first_name = excluded.first_name, "
            "last_name = excluded.last_name, username = excluded.username, "
            "designation = CASE WHEN excluded.designation != '' THEN excluded.designation ELSE people.designation END",
            (email, p["first_name"], p["last_name"], p["email"], p["username"], p["designation"], now_iso()),
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
            "INSERT INTO people (owner_email, first_name, last_name, email, username, designation, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (email, person["first_name"], person["last_name"], person["email"], person["username"],
             person["designation"], now_iso()),
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
                "UPDATE people SET first_name = ?, last_name = ?, email = ?, username = ?, designation = ? "
                "WHERE id = ? AND owner_email = ?",
                (person["first_name"], person["last_name"], person["email"], person["username"],
                 person["designation"], person_id, email),
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


# Sign-in, sign-up, SSO, /api/me and /api/logout live in auth.py (0.9.0).


# ---------- meetings ----------

@app.route("/api/meetings", methods=["GET"])
def list_meetings():
    """Newest first. Each meeting carries its task count and the state of its
    latest extraction job (queued / running / succeeded / failed)."""
    email = require_email()
    if not email:
        return jsonify({"error": "not identified"}), 401
    db = get_db()
    rows = db.execute(
        "SELECT meetings.*, (SELECT COUNT(*) FROM commitments WHERE commitments.meeting_id = meetings.id) AS commitment_count "
        "FROM meetings WHERE owner_email = ? ORDER BY meeting_date DESC, id DESC",
        (email,),
    ).fetchall()
    extraction = jobs.meeting_extraction_status(db, email)
    db.close()
    result = []
    for r in rows:
        item = dict(r)
        state = extraction.get(r["id"]) or {}
        item["extraction_status"] = state.get("status")
        item["extraction_error"] = state.get("error")
        result.append(item)
    return jsonify(result)


def create_meeting_row(db, owner_email, title, meeting_date, transcript_excerpt, source, transcript_provider=None):
    """Single insert point for a meeting row — manual upload, every real
    capture webhook, and the simulate button all go through this."""
    cur = db.execute(
        "INSERT INTO meetings (owner_email, title, meeting_date, created_at, source, transcript_excerpt, transcript_provider) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (owner_email, title, meeting_date, now_iso(), source, transcript_excerpt, transcript_provider),
    )
    return cur.lastrowid


BUILTIN_ENGINE_LABEL = "Built-in model"


def run_extraction(db, meeting_id, meeting_date, text=None, turns=None):
    """Extracts commitments from a transcript into the given meeting — real
    extraction, not the demo-seeded fixtures below. Every path uses this:
    pasted text, uploaded .txt/.docx/.pdf files, the capture and Fathom
    webhooks, and the manual re-extract button. `turns` is for a source that
    already hands us {"speaker","text"} pairs (Fathom), skipping the parser's
    regex turn splitting.

    The account's chosen engine runs first (an AI engine from the AI
    extraction page, if one is saved and confirmed); if it isn't chosen or
    fails, the built-in classifier (transcript_parser.py) runs.

    Returns {"count", "error", "engine"}: error is a user-facing string when
    extraction couldn't run (most commonly the model hasn't been trained —
    see src/ml/train_action_detector.py), never raised; engine names what
    actually did the work. Skips any utterance whose source_quote already
    has a commitment on this meeting, so re-running never duplicates rows."""
    if not text and not turns:
        return {"count": 0, "error": None, "engine": None}
    engine = BUILTIN_ENGINE_LABEL
    # [ai-extraction v0.5.0] Accounts that chose an AI engine are handled here;
    # None means "use the built-in path below, unchanged". Remove to revert.
    if ai_extraction is not None:
        ai_items = (ai_extraction.try_extract_from_turns(db, meeting_id, meeting_date, turns) if turns
                    else ai_extraction.try_extract(db, meeting_id, meeting_date, text))
        ai_label = ai_extraction.engine_label(db, meeting_id)
        if ai_items is not None:
            return {"count": _insert_extracted_commitments(db, meeting_id, ai_items), "error": None, "engine": ai_label}
        if ai_label:
            engine = f"{BUILTIN_ENGINE_LABEL} ({ai_label} failed — see the AI extraction page)"
    # [/ai-extraction v0.5.0]
    if transcript_parser is None:
        return {"count": 0, "error": "commitment extraction isn't installed — see requirements.txt", "engine": None}
    try:
        if turns:
            extracted = transcript_parser.extract_commitments_from_turns(turns, meeting_date=meeting_date)
        else:
            extracted = transcript_parser.extract_commitments(text, meeting_date=meeting_date)
    except transcript_parser.ModelNotReadyError as e:
        return {"count": 0, "error": str(e), "engine": None}
    return {"count": _insert_extracted_commitments(db, meeting_id, extracted), "error": None, "engine": engine}


def auto_extract_commitments(db, meeting_id, meeting_date, transcript_excerpt):
    """(count, error) form of run_extraction for raw transcript text."""
    outcome = run_extraction(db, meeting_id, meeting_date, text=transcript_excerpt)
    return outcome["count"], outcome["error"]


def auto_extract_commitments_from_turns(db, meeting_id, meeting_date, turns):
    """(count, error) form of run_extraction for speaker turns (Fathom)."""
    outcome = run_extraction(db, meeting_id, meeting_date, turns=turns)
    return outcome["count"], outcome["error"]


# ---------- background jobs (jobs.py) ----------

def _job_changed(user_email, job):
    notify(user_email, "job_updated", job_id=job["id"], job_kind=job["kind"], status=job["status"],
           meeting_id=job.get("meeting_id"))


jobs.init(get_db, _job_changed)


@jobs.handler("extract_commitments", "Extract commitments")
def job_extract_commitments(payload, job):
    """Extracts a meeting's commitments, then pushes them to the Google
    Sheet if auto-sync is on. A failed sheet push is queued again as its own
    retrying job rather than failing the extraction."""
    db = get_db()
    meeting = db.execute(
        "SELECT id, owner_email, meeting_date, transcript_excerpt FROM meetings WHERE id = ?", (payload.get("meeting_id"),)
    ).fetchone()
    if meeting is None:
        db.close()
        raise jobs.PermanentError("the meeting was deleted before extraction ran")
    turns = payload.get("turns")
    outcome = run_extraction(db, meeting["id"], meeting["meeting_date"],
                             text=None if turns else meeting["transcript_excerpt"], turns=turns)
    db.commit()
    db.close()
    if outcome["error"]:
        raise jobs.PermanentError(outcome["error"])
    email = meeting["owner_email"]
    notify(email, "commitments_changed", meeting_id=meeting["id"], extracted=outcome["count"])
    synced, sync_error = auto_sync_to_sheet(email, meeting["id"])
    if sync_error:
        queue_sheet_retry(email, meeting["id"])
    elif synced:
        notify(email, "commitments_changed", meeting_id=meeting["id"], synced=synced)
    return {"commitments_extracted": outcome["count"], "extraction_engine": outcome["engine"],
            "synced_to_tracker": synced, "tracker_sync_error": sync_error}


@jobs.handler("sync_sheet", "Sync to Google Sheet")
def job_sync_sheet(payload, job):
    """Retries writing not-yet-synced commitments to the sheet (backoff in jobs.py)."""
    email = job["user_email"]
    count, error = auto_sync_to_sheet(email, payload.get("meeting_id"))
    if error and ("No module named" in error or "isn't installed" in error):
        # Retrying can't install a library — say how to fix it instead.
        raise jobs.PermanentError(f"{error} — start the app with the project's Python "
                                  "(..\\.venv\\Scripts\\python src\\app.py) or pip install -r requirements.txt")
    if error:
        raise RuntimeError(error)
    if count:
        notify(email, "commitments_changed", synced=count)
    return {"synced_to_tracker": count}


@jobs.handler("run_due_nudges", "Send due reminders")
def job_run_due_nudges(payload, job):
    results = run_nudges(job["user_email"])
    return {"sent": sum(r["status"] == "sent" for r in results), "skipped": sum(r["status"] == "skipped" for r in results),
            "failed": sum(r["status"] == "failed" for r in results)}


def queue_sheet_retry(email, meeting_id=None):
    db = get_db()
    job_id = jobs.enqueue(db, email, "sync_sheet", {"meeting_id": meeting_id}, meeting_id=meeting_id,
                          max_attempts=5, delay_seconds=60)
    db.commit()
    db.close()
    return job_id


def queue_extraction(db, email, meeting_id, turns=None):
    """Queues extraction for a meeting the caller just inserted, commits, and
    returns the fields for the JSON response: the result when jobs run
    inline (tests), otherwise the job id and "queued"."""
    job_id = jobs.enqueue(db, email, "extract_commitments", {"meeting_id": meeting_id, "turns": turns},
                          meeting_id=meeting_id, max_attempts=2)
    db.commit()
    job = jobs.after_commit(job_id)
    if job is None:
        return {"job_id": job_id, "extraction": "queued", "commitments_extracted": None, "extraction_error": None,
                "extraction_engine": None, "synced_to_tracker": None, "tracker_sync_error": None}
    result = job["result"] or {}
    return {
        "job_id": job_id,
        "extraction": job["status"],
        "commitments_extracted": result.get("commitments_extracted", 0),
        "extraction_error": job["last_error"] if job["status"] == "failed" else None,
        "extraction_engine": result.get("extraction_engine"),
        "synced_to_tracker": result.get("synced_to_tracker", 0),
        "tracker_sync_error": result.get("tracker_sync_error"),
    }


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
        "SELECT id, first_name, last_name, email, username, designation FROM people WHERE owner_email = ? "
        "ORDER BY last_name, first_name",
        (account_email,),
    ).fetchall()]


def designation_for(db, account_email, person_email):
    """A task owner's designation from the account's People directory, or ''."""
    if not person_email:
        return ""
    row = db.execute("SELECT designation FROM people WHERE owner_email = ? AND email = ?",
                     (account_email, person_email.lower())).fetchone()
    return (row["designation"] or "") if row else ""


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
    """Manual transcript upload. Either JSON {title, meeting_date,
    transcript_excerpt} (pasted text) or multipart/form-data with the same
    fields plus `file` — a .txt, .docx or .pdf transcript, turned into text
    by document_text.py. Commitments are then extracted by a background job
    with the account's chosen engine (built-in model or AI), exactly as for
    pasted text; the response says whether that's queued or, when jobs run
    inline, how it went."""
    email = require_email()
    if not email:
        return jsonify({"error": "not identified"}), 401

    data = request.form if request.files or request.form else (request.get_json(force=True) or {})
    title = (data.get("title") or "").strip()
    meeting_date = (data.get("meeting_date") or "").strip() or today_str()
    transcript_excerpt = (data.get("transcript_excerpt") or "").strip() or None
    upload = request.files.get("file")
    file_kind = None

    if not title:
        return jsonify({"error": "title is required"}), 400
    if upload and upload.filename:
        try:
            transcript_excerpt, file_kind = document_text.extract_text(upload.filename, upload.read())
        except document_text.DocumentError as e:
            return jsonify({"error": f'couldn\'t read "{upload.filename}": {e}'}), 400
    elif transcript_excerpt and looks_like_binary_text(transcript_excerpt):
        return jsonify({"error": "transcript_excerpt looks like a binary file (e.g. .docx/.pdf) — upload it as a file "
                                 "so it can be converted, or paste plain text"}), 400

    db = get_db()
    meeting_id = create_meeting_row(db, email, title, meeting_date, transcript_excerpt, source="upload")
    notify(email, "meeting_created", meeting_id=meeting_id, source="upload")
    if not transcript_excerpt:
        db.commit()
        db.close()
        return jsonify({"id": meeting_id, "job_id": None, "extraction": None, "commitments_extracted": 0,
                        "extraction_error": None, "extraction_engine": None, "synced_to_tracker": 0,
                        "tracker_sync_error": None, "file_kind": None, "transcript_chars": 0}), 201
    outcome = queue_extraction(db, email, meeting_id)
    db.close()
    return jsonify({"id": meeting_id, **outcome, "file_kind": file_kind,
                    "transcript_chars": len(transcript_excerpt)}), 201


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

    outcome = queue_extraction(db, email, meeting_id)
    db.close()
    if outcome["extraction_error"]:
        return jsonify({"error": outcome["extraction_error"], "job_id": outcome["job_id"]}), 503
    if outcome["extraction"] == "queued":
        return jsonify({"ok": True, **outcome}), 202
    return jsonify({"ok": True, **outcome}), 201


# ---------- clearing meetings and tasks ----------

def _delete_commitments(db, where_sql, params):
    """Deletes the matching commitments and their nudge / tracker history.
    where_sql filters `commitments` (joined to its meeting as `m`)."""
    ids_sql = f"SELECT commitments.id FROM commitments JOIN meetings m ON m.id = commitments.meeting_id WHERE {where_sql}"
    db.execute(f"DELETE FROM nudge_log WHERE commitment_id IN ({ids_sql})", params)
    db.execute(f"DELETE FROM tracker_sync_log WHERE commitment_id IN ({ids_sql})", params)
    return db.execute(f"DELETE FROM commitments WHERE id IN ({ids_sql})", params).rowcount


def _delete_meetings(db, email, meeting_ids):
    """Deletes meetings (and their tasks) owned by this account. Their
    transcript-source log entries are kept but unlinked; queued extraction
    jobs for them are cancelled. Returns (meetings, tasks) deleted."""
    if not meeting_ids:
        return 0, 0
    marks = ",".join("?" * len(meeting_ids))
    tasks = _delete_commitments(db, f"m.owner_email = ? AND m.id IN ({marks})", (email, *meeting_ids))
    db.execute(f"UPDATE transcript_sync_log SET meeting_id = NULL WHERE meeting_id IN ({marks})", meeting_ids)
    jobs.cancel_for_meetings(db, meeting_ids)
    meetings = db.execute(f"DELETE FROM meetings WHERE owner_email = ? AND id IN ({marks})", (email, *meeting_ids)).rowcount
    return meetings, tasks


@app.route("/api/meetings/<int:meeting_id>", methods=["DELETE"])
def delete_meeting(meeting_id):
    email = require_email()
    if not email:
        return jsonify({"error": "not identified"}), 401
    db = get_db()
    meetings, tasks = _delete_meetings(db, email, [meeting_id])
    if not meetings:
        db.close()
        return jsonify({"error": "not found"}), 404
    db.commit()
    db.close()
    notify(email, "meetings_changed", deleted=[meeting_id])
    return jsonify({"ok": True, "meetings_deleted": meetings, "commitments_deleted": tasks})


@app.route("/api/meetings/clear", methods=["POST"])
def clear_meetings():
    """Deletes every meeting of this account, with all their tasks. Body must
    include {"confirm": true}. Rows already written to a Google Sheet or
    another tracker stay there — only this app's copy is removed."""
    email = require_email()
    if not email:
        return jsonify({"error": "not identified"}), 401
    if (request.get_json(force=True) or {}).get("confirm") is not True:
        return jsonify({"error": 'send {"confirm": true} to clear all meetings'}), 400
    db = get_db()
    ids = [r["id"] for r in db.execute("SELECT id FROM meetings WHERE owner_email = ?", (email,)).fetchall()]
    meetings, tasks = _delete_meetings(db, email, ids)
    db.commit()
    db.close()
    notify(email, "meetings_changed", cleared=True)
    return jsonify({"ok": True, "meetings_deleted": meetings, "commitments_deleted": tasks})


@app.route("/api/commitments/clear", methods=["POST"])
def clear_commitments():
    """Deletes tasks but keeps the meetings (their transcripts can be
    re-extracted later). Body: {"confirm": true, "meeting_id": optional} —
    with meeting_id only that meeting's tasks go, otherwise all of them."""
    email = require_email()
    if not email:
        return jsonify({"error": "not identified"}), 401
    data = request.get_json(force=True) or {}
    if data.get("confirm") is not True:
        return jsonify({"error": 'send {"confirm": true} to clear tasks'}), 400
    db = get_db()
    if data.get("meeting_id") is not None:
        owned = db.execute("SELECT 1 FROM meetings WHERE id = ? AND owner_email = ?", (data["meeting_id"], email)).fetchone()
        if not owned:
            db.close()
            return jsonify({"error": "meeting not found"}), 404
        tasks = _delete_commitments(db, "m.owner_email = ? AND m.id = ?", (email, data["meeting_id"]))
    else:
        tasks = _delete_commitments(db, "m.owner_email = ?", (email,))
    db.commit()
    db.close()
    notify(email, "commitments_changed", cleared=tasks)
    return jsonify({"ok": True, "commitments_deleted": tasks})


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
    notify(email, "meeting_created", source="demo")
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
    meeting_filter = request.args.get("meeting_id", type=int)

    query = (
        "SELECT commitments.*, meetings.title AS meeting_title "
        "FROM commitments JOIN meetings ON meetings.id = commitments.meeting_id "
        "WHERE meetings.owner_email = ?"
    )
    params = [email]
    if meeting_filter:
        query += " AND commitments.meeting_id = ?"
        params.append(meeting_filter)
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
    notify(email, "commitments_changed", meeting_id=meeting_id)
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

    """Edits any of task_text, owner_name, owner_email, due_date, status. An
    owner_email given here is a manual pick that re-matching never
    overwrites (an empty string clears it). The change is stamped with
    updated_at and, when the Google Sheet auto-syncs, the whole row is
    rewritten there (0.10.0 — before, only status was pushed)."""
    email = require_email()
    if not email:
        return jsonify({"error": "not identified"}), 401

    data = request.get_json(force=True) or {}
    changes, error = clean_commitment_changes(data)
    if error:
        return jsonify({"error": error}), 400
    if not changes:
        return jsonify({"error": "nothing to change — send task_text, owner_name, owner_email, due_date or status"}), 400

    db = get_db()
    row = get_commitment_or_404(db, email, commitment_id)
    if row is None:
        db.close()
        return jsonify({"error": "not found"}), 404

    changed = apply_commitment_changes(db, email, row, changes)
    db.commit()
    db.close()
    tracker_sync_error = None
    if changed:
        notify(email, "commitments_changed", commitment_id=commitment_id)
        tracker_sync_error = auto_push_row_to_sheet(email, commitment_id)
    return jsonify({"ok": True, "changed": changed, "tracker_sync_error": tracker_sync_error})


COMMITMENT_STATUSES = ("pending", "in_progress", "done")


def parse_due_date(value):
    """(iso date or None, error). Accepts YYYY-MM-DD (optionally followed by
    a time, as some exports add) or blank for "no due date"."""
    text = str(value or "").strip()
    if not text:
        return None, None
    try:
        return date.fromisoformat(text[:10]).isoformat(), None
    except ValueError:
        return None, f"'{text}' isn't a date — use YYYY-MM-DD, e.g. 2026-10-31"


def clean_commitment_changes(data, from_tracker=False):
    """Validates an edit to a commitment. App edits use the API field names
    (task_text, owner_name, ...); a tracker edit (from_tracker=True) uses the
    sheet's (task, owner, owner_email, due_date, status) and may write status
    as "Done", "In progress", "Closed"... Only keys present are changed.
    Returns (changes, error)."""
    names = ({"task": "task_text", "owner": "owner_name", "owner_email": "owner_email", "due_date": "due_date",
              "status": "status"} if from_tracker else
             {k: k for k in ("task_text", "owner_name", "owner_email", "due_date", "status")})
    changes = {}
    for src, field in names.items():
        if src not in data or data[src] is None:
            continue
        value = data[src]
        if field == "task_text":
            value = str(value).strip()
            if not value:
                return None, "the task can't be blank"
            changes[field] = value[:300]
        elif field == "owner_name":
            value = str(value).strip()
            if not value:
                return None, "the owner can't be blank"
            changes[field] = value[:120]
        elif field == "owner_email":
            value = str(value).strip().lower()
            if value and not people_directory.EMAIL_RE.match(value):
                return None, f"'{value}' doesn't look like an email address"
            changes[field] = value or None
        elif field == "due_date":
            iso, error = parse_due_date(value)
            if error:
                return None, error
            changes[field] = iso
        elif field == "status":
            status = normalize_tracker_status(value) if from_tracker else value
            if status not in COMMITMENT_STATUSES:
                return None, ("status must be one of pending, in_progress, done"
                              + (" (or a synonym like Open, In progress, Closed)" if from_tracker else ""))
            changes[field] = status
    return changes, None


def apply_commitment_changes(db, account_email, row, changes):
    """Writes the fields that actually differ and stamps updated_at. A new
    owner name with no explicit email is matched to People again (unless the
    email was picked by hand). Returns the list of changed field names."""
    current = dict(row)
    updates = {}
    for field in ("task_text", "owner_name", "due_date", "status"):
        if field in changes and changes[field] != current.get(field):
            updates[field] = changes[field]
    email_given = "owner_email" in changes and changes["owner_email"] != current.get("owner_email")
    if email_given:
        updates["owner_email"] = changes["owner_email"]
        updates["owner_match"] = "manual" if changes["owner_email"] else None
    elif "owner_name" in updates and current.get("owner_match") != "manual":
        found, match, _ = people_directory.match_owner(updates["owner_name"], load_people(db, account_email))
        updates["owner_email"], updates["owner_match"] = found, match
    changed = [f for f in updates if f != "owner_match"]
    if not changed:
        return []
    updates["updated_at"] = now_iso()
    db.execute(f"UPDATE commitments SET {', '.join(f'{f} = ?' for f in updates)} WHERE id = ?",
               (*updates.values(), current["id"]))
    return changed


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


def write_email_nudge(account_email, config, commitment, reason):
    """The email for one reminder: the account's draft with its placeholders
    filled in, rewritten by AI for the recipient's designation when AI
    tailoring is on (ai_writer.py). Never raises."""
    db = get_db()
    try:
        designation = designation_for(db, account_email, commitment.get("owner_email"))
        return ai_writer.personalise(db, account_email, config, commitment, reason, designation,
                                     config.get("sender_name") or account_email)
    finally:
        db.close()


def send_and_log(setting, commitment, reason):
    """Delivers one reminder and records the result in nudge_log."""
    try:
        config = secret_store.load_config(setting["config_json"])
        webhook_or_key = secret_store.decrypt(setting["webhook_or_key"])
    except secret_store.SecretUnreadable as e:
        status, detail = "failed", str(e)
    else:
        if setting["channel"] == "email" and commitment.get("owner_email"):
            written = write_email_nudge(setting["user_email"], config, commitment, reason)
            status, detail = notifications.deliver(setting["channel"], webhook_or_key, config, commitment, reason,
                                                   message=(written["subject"], written["body"]))
            if written["note"] and status == "sent":
                detail = f"{detail} — {written['note']}"
        else:
            status, detail = notifications.deliver(setting["channel"], webhook_or_key, config, commitment, reason)
    db = get_db()
    db.execute(
        "INSERT INTO nudge_log (commitment_id, triggered_at, channel, status, reason, detail) VALUES (?, ?, ?, ?, ?, ?)",
        (commitment["id"], now_iso(), setting["channel"], status, reason, detail),
    )
    db.commit()
    db.close()
    notify(setting["user_email"], "nudge_sent", commitment_id=commitment["id"], channel=setting["channel"], status=status)
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


NUDGE_CHECK_INTERVAL_SECONDS = int(os.environ.get("NUDGE_CHECK_INTERVAL_SECONDS", "900"))


def queue_due_nudge_runs():
    """Periodic task on the job worker: queues one 'run_due_nudges' job per
    account that switched on automatic nudges (skipping an account whose
    previous run is still waiting). Each job retries on its own if it fails."""
    db = get_db()
    jobs.ensure_schema(db)
    accounts = [r["user_email"] for r in db.execute(
        "SELECT user_email FROM nudge_preferences WHERE auto_enabled = 1").fetchall()]
    queued = 0
    for account in accounts:
        waiting = db.execute("SELECT 1 FROM jobs WHERE user_email = ? AND kind = 'run_due_nudges' "
                             "AND status IN ('queued', 'running') LIMIT 1", (account,)).fetchone()
        if not waiting:
            jobs.enqueue(db, account, "run_due_nudges", {}, max_attempts=2)
            queued += 1
    db.commit()
    db.close()
    return queued


jobs.add_periodic("automatic nudges", NUDGE_CHECK_INTERVAL_SECONDS, queue_due_nudge_runs)

_services_started = False


def start_background_services():
    """Starts the job worker (which also runs the nudge check) once per process."""
    global _services_started
    if _services_started:
        return
    _services_started = True
    if jobs.start():
        print(f"[nudges] automatic nudge checks every {NUDGE_CHECK_INTERVAL_SECONDS}s (for accounts that switched them on)")


def _mask(value):
    value = value or ""
    return ("••••" + value[-4:]) if len(value) > 4 else ("••••" if value else "")


# Credential fields inside tracker / transcript-source configs. Never sent
# back to the browser: GET returns them blank plus a masked hint, and a blank
# field on save keeps the stored value.
SECRET_FIELDS = {"api_token", "api_key", "client_secret", "webhook_secret_token", "webhook_secret", "password"}


def _hide_secrets(config):
    shown = dict(config)
    hints = {}
    for field in SECRET_FIELDS & shown.keys():
        if shown[field]:
            hints[field] = _mask(shown[field])
        shown[field] = ""
    return shown, hints


def _keep_blank_secrets(config, previous):
    for field in SECRET_FIELDS:
        if not (config.get(field) or "").strip() and previous.get(field):
            config[field] = previous[field]
    return config


def _stored_settings(db, table, email):
    """(provider, decrypted config) of a user's tracker / transcript-source
    row, or (None, {}) if there is none or it can't be decrypted."""
    row = db.execute(f"SELECT provider, config_json FROM {table} WHERE user_email = ?", (email,)).fetchone()
    if row is None:
        return None, {}
    return row["provider"], secret_store.load_config(row["config_json"], default_on_error=True)


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
        item.pop("config_json")
        try:
            config = secret_store.load_config(r["config_json"])
            webhook_or_key = secret_store.decrypt(r["webhook_or_key"])
        except secret_store.SecretUnreadable as e:
            config, webhook_or_key, item["unreadable"] = {}, "", str(e)
        item["has_password"] = bool(config.pop("password", ""))
        item["config"] = config
        item["webhook_or_key"] = webhook_or_key if r["channel"] == "email" else _mask(webhook_or_key)
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
    previous = secret_store.load_config(existing["config_json"], default_on_error=True) if existing else {}
    try:
        previous_key = secret_store.decrypt(existing["webhook_or_key"]) if existing else ""
    except secret_store.SecretUnreadable:
        previous_key = ""

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
            **_email_draft_fields(incoming),
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
        webhook_or_key = webhook_or_key or previous_key
        if not webhook_or_key:
            db.close()
            return jsonify({"error": "webhook_or_key is required"}), 400

    db.execute(
        "INSERT INTO nudge_settings (user_email, channel, custom_name, webhook_or_key, cadence, enabled, config_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(user_email, channel) DO UPDATE SET "
        "custom_name = excluded.custom_name, webhook_or_key = excluded.webhook_or_key, "
        "cadence = excluded.cadence, enabled = excluded.enabled, config_json = excluded.config_json",
        (email, channel, custom_name, secret_store.encrypt(webhook_or_key), cadence, enabled,
         secret_store.dump_config(config)),
    )
    db.commit()
    db.close()
    return jsonify({"ok": True}), 201


def _email_draft_fields(incoming):
    """The email channel's reminder draft (ai_writer.py). Blank = the default draft."""
    return {
        "draft_subject": (incoming.get("draft_subject") or "").strip()[:200],
        "draft_body": (incoming.get("draft_body") or "").strip()[:5000],
        "sender_name": (incoming.get("sender_name") or "").strip()[:120],
        "ai_mode": "extraction_engine" if incoming.get("ai_mode") == "extraction_engine" else "off",
    }


@app.route("/api/email-draft/defaults", methods=["GET"])
def email_draft_defaults():
    return jsonify({"subject": ai_writer.DEFAULT_SUBJECT, "body": ai_writer.DEFAULT_BODY,
                    "placeholders": ai_writer.PLACEHOLDERS})


@app.route("/api/email-draft/preview", methods=["POST"])
def email_draft_preview():
    """Shows what one reminder email would look like — for a real task
    (commitment_id) or a sample, addressed to someone with the given
    designation. With AI tailoring on, this makes one call to the AI engine
    saved on the AI extraction page. Nothing is sent."""
    email = require_email()
    if not email:
        return jsonify({"error": "not identified"}), 401
    data = request.get_json(force=True) or {}
    config = _email_draft_fields(data.get("config") or {})
    db = get_db()
    commitment = None
    if data.get("commitment_id"):
        row = get_commitment_or_404(db, email, data["commitment_id"])
        commitment = dict(row) if row else None
    if commitment is None:
        commitment = {"task_text": "Send the revised budget model to the finance team", "owner_name": "Kevin Heys",
                      "owner_email": "kevin.heys@example.com", "due_date": (date.today() + timedelta(days=1)).isoformat(),
                      "status": "pending", "meeting_title": "FY2027 Financial Planning"}
    designation = (data.get("designation") or "").strip() or designation_for(db, email, commitment.get("owner_email"))
    written = ai_writer.personalise(db, email, config, commitment, data.get("reason") or "day_before", designation,
                                    config.get("sender_name") or email)
    db.close()
    return jsonify({**written, "designation": designation, "to": commitment.get("owner_email"),
                    "task": commitment.get("task_text")})


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
    try:
        config = secret_store.load_config(setting["config_json"])
        webhook_or_key = secret_store.decrypt(setting["webhook_or_key"])
    except secret_store.SecretUnreadable as e:
        return jsonify({"ok": False, "status": "failed", "detail": str(e)}), 502
    message = None
    if channel == "email":
        written = write_email_nudge(email, config, sample, "test")
        message = (written["subject"], written["body"])
    status, detail = notifications.deliver(setting["channel"], webhook_or_key, config, sample, "test",
                                           **({"message": message} if message else {}))
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
        return jsonify({"provider": "none", "custom_name": None, "config": {}, "secret_hints": {}, "enabled": True})
    unreadable = None
    try:
        config = secret_store.load_config(row["config_json"])
    except secret_store.SecretUnreadable as e:
        config, unreadable = {}, str(e)
    config, hints = _hide_secrets(config)
    return jsonify({
        "provider": row["provider"],
        "custom_name": row["custom_name"],
        "config": config,
        "secret_hints": hints,
        "enabled": bool(row["enabled"]),
        "unreadable": unreadable,
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

    db = get_db()
    previous_provider, previous = _stored_settings(db, "tracker_settings", email)
    if previous_provider == provider:
        _keep_blank_secrets(config, previous)

    if provider != "none":
        missing = [f for f in TRACKER_REQUIRED_FIELDS[provider] if not (config.get(f) or "").strip()]
        if missing:
            db.close()
            return jsonify({"error": f"missing required field(s) for {provider}: {', '.join(missing)}"}), 400

    db.execute(
        "INSERT INTO tracker_settings (user_email, provider, custom_name, config_json, enabled) "
        "VALUES (?, ?, ?, ?, ?) "
        "ON CONFLICT(user_email) DO UPDATE SET "
        "provider = excluded.provider, custom_name = excluded.custom_name, "
        "config_json = excluded.config_json, enabled = excluded.enabled",
        (email, provider, custom_name, secret_store.dump_config(config), enabled),
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
    try:
        config = secret_store.load_config(tracker["config_json"])
    except secret_store.SecretUnreadable:
        db.close()  # release the write lock taken above before the 409 handler runs
        raise
    external_ref = row["external_ref"]

    if provider == "google_sheets":
        if google_sheets is None:
            db.close()
            return jsonify({"error": "Google Sheets integration isn't installed — see requirements.txt"}), 503
        try:
            if external_ref:
                try:
                    google_sheets.update_row(config.get("spreadsheet_id"), external_ref, dict(row))
                    detail = f"Updated {external_ref} (all fields) in Google Sheet {config.get('spreadsheet_id')}"
                except google_sheets.RowMissingError:
                    external_ref = None   # its row was deleted from the sheet: add it again below
            if not external_ref:
                external_ref = google_sheets.append_commitment_row(config.get("spreadsheet_id"), dict(row))
                db.execute("UPDATE commitments SET external_ref = ? WHERE id = ?", (external_ref, commitment_id))
                detail = f"Wrote {external_ref} to Google Sheet {config.get('spreadsheet_id')}"
                if row["external_ref"]:
                    detail += f" (its earlier row {row['external_ref']} was no longer in the sheet)"
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
    notify(email, "commitments_changed", commitment_id=commitment_id)
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
    return secret_store.load_config(tracker["config_json"], default_on_error=True) or None


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


def write_row_to_sheet(db, config, commitment_id, detail):
    """Rewrites one synced commitment's whole sheet row (task, owner, due
    date, status, owner email) and logs it. Returns an error string or None."""
    row = db.execute("SELECT * FROM commitments WHERE id = ?", (commitment_id,)).fetchone()
    if row is None or not row["external_ref"] or google_sheets is None:
        return None
    try:
        google_sheets.update_row(config.get("spreadsheet_id"), row["external_ref"], dict(row))
    except google_sheets.RowMissingError:
        # The row was deleted (or the sheet cleared). Unlink rather than write
        # into another task's row; "Sync to tracker" / "Sync all" adds it again.
        db.execute("UPDATE commitments SET external_ref = NULL WHERE id = ?", (commitment_id,))
        db.execute(
            "INSERT INTO tracker_sync_log (commitment_id, provider, direction, triggered_at, status, detail) "
            "VALUES (?, 'google_sheets', 'outbound', ?, 'skipped', ?)",
            (commitment_id, now_iso(), f"{row['external_ref']} is no longer in the sheet — unlinked; "
                                       "use Sync to tracker or Sync all to add it again"),
        )
        return None
    except Exception as e:
        return f"Couldn't update the Google Sheet: {e}"
    db.execute(
        "INSERT INTO tracker_sync_log (commitment_id, provider, direction, triggered_at, status, detail) "
        "VALUES (?, 'google_sheets', 'outbound', ?, 'synced', ?)",
        (commitment_id, now_iso(), detail.format(ref=row["external_ref"])),
    )
    return None


def auto_push_row_to_sheet(email, commitment_id):
    """Mirrors an in-app edit (any field) onto the commitment's sheet row, if
    it's already synced and auto-sync is on. Returns an error string or None."""
    db = get_db()
    config = get_sheets_tracker_config(db, email)
    if not config or not config.get("auto_sync"):
        db.close()
        return None
    error = write_row_to_sheet(db, config, commitment_id, "Updated {ref} in the sheet after an edit in the app")
    if error:
        db.close()
        return error
    db.commit()
    db.close()
    return None


@app.route("/api/tracker/sync-all", methods=["POST"])
def sync_all_to_tracker():
    """Pushes every not-yet-synced commitment to the Google Sheet in one call
    — the bulk alternative to clicking "Sync to tracker" per commitment.
    Works whether or not auto-sync is on (it's how you catch up the backlog
    that existed before auto-sync was enabled).

    It first checks which already-linked tasks still have a row in the sheet
    and re-adds those whose row was deleted (0.10.2) — so emptying the sheet
    and pressing "Sync all" rebuilds it, numbered from 1."""
    email = require_email()
    if not email:
        return jsonify({"error": "not identified"}), 401

    db = get_db()
    db.execute("BEGIN IMMEDIATE")
    config = get_sheets_tracker_config(db, email)
    if not config:
        db.close()
        return jsonify({"error": "bulk sync needs Google Sheets connected as your task tracker (Integrations)"}), 400
    relinked = 0
    if google_sheets is not None:
        linked = db.execute(
            "SELECT commitments.id, commitments.external_ref FROM commitments JOIN meetings ON meetings.id = commitments.meeting_id "
            "WHERE meetings.owner_email = ? AND commitments.external_ref IS NOT NULL", (email,)).fetchall()
        if linked:
            try:
                gone = google_sheets.missing_refs(config.get("spreadsheet_id"), [r["external_ref"] for r in linked])
            except Exception as e:
                db.close()
                return jsonify({"error": f"Google Sheets API call failed: {e}"}), 502
            for r in linked:
                if r["external_ref"] in gone:
                    db.execute("UPDATE commitments SET external_ref = NULL WHERE id = ?", (r["id"],))
                    relinked += 1
    count, error = push_unsynced_to_sheet(db, email, config)
    if error:
        db.close()
        return jsonify({"error": error}), 502
    db.commit()
    db.close()
    notify(email, "commitments_changed", synced=count)
    return jsonify({"ok": True, "synced": count, "readded": relinked}), 201


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
    """The UI's simulate button: applies a status change as if it came from
    the external tracker, and logs it as an inbound sync event."""
    new_status = normalize_tracker_status(new_status)
    if new_status is None:
        return None, "status must be one of pending, in_progress, done (or a synonym like Open, In progress, Closed)"

    row = db.execute("SELECT * FROM commitments WHERE id = ?", (commitment_id,)).fetchone()
    owner_email = db.execute("SELECT owner_email FROM meetings WHERE id = ?", (row["meeting_id"],)).fetchone()["owner_email"]
    apply_commitment_changes(db, owner_email, row, {"status": new_status})
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
    notify(email, "commitment_status", commitment_id=commitment_id, detail=result["detail"])
    return jsonify({"ok": True, **result}), 201


def legacy_tracker_webhook_enabled():
    return os.environ.get("CT_ALLOW_UNSIGNED_TRACKER_WEBHOOK") == "1"


def _webhook_account(token):
    """(db, account email) for a webhook URL token, or (db, None)."""
    db = get_db()
    row = db.execute("SELECT user_email FROM transcript_source_settings WHERE webhook_token = ?", (token,)).fetchone()
    return db, (row["user_email"] if row else None)


FIELD_LABELS = {"task_text": "task", "owner_name": "owner", "owner_email": "owner email", "due_date": "due date",
                "status": "status"}


def _edit_time(value):
    """A tracker edit's time as a naive-UTC datetime: unix seconds or
    milliseconds, or an ISO timestamp. None if missing or unreadable."""
    if value in (None, ""):
        return None
    try:
        number = float(value)
        return datetime.utcfromtimestamp(number / 1000 if number > 10_000_000_000 else number)
    except (TypeError, ValueError, OverflowError, OSError):
        pass
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc).replace(tzinfo=None) if parsed.tzinfo else parsed
    except ValueError:
        return None


def _apply_tracker_payload(db, email, data, provider_default="external", sent_at=None):
    """Applies an edit made in the tracker (0.10.0: any of task, owner,
    owner_email, due_date, status — keys left out stay as they are).

    Most recent edit wins: the edit's time (`edited_at` in the body, else
    the signed request's timestamp, else now) is compared with the task's
    last change in the app (updated_at). An older edit is ignored, and for
    Google Sheets the app's version is written back to the row so both sides
    show the winner. After an accepted edit, cells the app normalised — a
    status typed as "Closed", an owner email found from People — are written
    back too. Writes through the API don't fire the sheet's On edit trigger,
    so none of this can loop."""
    external_ref = (data.get("external_ref") or "").strip()
    provider = data.get("provider") or provider_default
    if not external_ref:
        return None, ({"error": "external_ref is required"}, 400)
    row = None
    # The 0.10.2 Apps Script sends "sheet-<number in column A>" plus the row
    # number as row_ref, so rows synced before 0.10.2 ("row-<n>") still match.
    for ref in (external_ref, (data.get("row_ref") or "").strip()):
        if ref and row is None:
            row = db.execute(
                "SELECT commitments.* FROM commitments JOIN meetings ON meetings.id = commitments.meeting_id "
                "WHERE meetings.owner_email = ? AND commitments.external_ref = ?",
                (email, ref),
            ).fetchone()
    if row is None:
        return None, ({"error": "no matching commitment for that external_ref"}, 404)
    changes, error = clean_commitment_changes(data, from_tracker=True)
    if error:
        return None, ({"error": error}, 400)
    if not changes:
        return None, ({"error": "nothing to update — send task, owner, owner_email, due_date or status"}, 400)

    edited = _edit_time(data.get("edited_at")) or _edit_time(sent_at) or datetime.utcnow()
    app_changed = datetime.fromisoformat(row["updated_at"] or row["created_at"])
    sheet_config = get_sheets_tracker_config(db, email) if provider == "google_sheets" else None
    triggered_at = now_iso()

    if edited < app_changed:
        detail = (f"Ignored an edit from {provider} made at {edited.isoformat(timespec='seconds')} UTC — "
                  f"the app's change at {row['updated_at'] or row['created_at']} UTC is newer and wins")
        if sheet_config:
            write_error = write_row_to_sheet(db, sheet_config, row["id"], "Restored {ref} to the app's newer version")
            if write_error:
                detail += f" ({write_error})"
        db.execute(
            "INSERT INTO tracker_sync_log (commitment_id, provider, direction, triggered_at, status, detail) "
            "VALUES (?, ?, 'inbound', ?, 'skipped', ?)",
            (row["id"], provider, triggered_at, detail),
        )
        return {"commitment_id": row["id"], "applied": False, "changed": [], "triggered_at": triggered_at,
                "detail": detail}, None

    changed = apply_commitment_changes(db, email, row, changes)
    detail = (f"Updated {', '.join(FIELD_LABELS[f] for f in changed)} via {provider}" if changed
              else f"No change — {provider} sent the values the app already has")
    if "status" in changed:
        detail += f" (status '{changes['status']}')"
    db.execute(
        "INSERT INTO tracker_sync_log (commitment_id, provider, direction, triggered_at, status, detail) "
        "VALUES (?, ?, 'inbound', ?, 'received', ?)",
        (row["id"], provider, triggered_at, detail),
    )
    if sheet_config and google_sheets is not None:
        stored = db.execute("SELECT * FROM commitments WHERE id = ?", (row["id"],)).fetchone()
        sent = {"task_text": data.get("task"), "owner_name": data.get("owner"), "due_date": data.get("due_date"),
                "status": data.get("status"), "owner_email": data.get("owner_email")}
        shown = dict(zip(("task_text", "owner_name", "due_date", "status", "owner_email"),
                         google_sheets.row_values(dict(stored))[1:]))
        if any(sent[k] is not None and str(sent[k]).strip() != str(shown[k]) for k in sent):
            write_row_to_sheet(db, sheet_config, row["id"], "Tidied {ref} in the sheet to match the app")
    return {"commitment_id": row["id"], "applied": True, "changed": changed, "triggered_at": triggered_at,
            "detail": detail}, None


@app.route("/api/webhooks/tracker-update/<token>", methods=["POST"])
def tracker_webhook(token):
    """Real, functional inbound endpoint — the two-way half of the tracker
    integration. Point a Jira Automation rule or a Google Apps Script
    trigger at this URL when a task's status changes there, and the
    matching commitment here gets updated. This route only *receives*.

    Not session-protected — an external service has no browser session.
    The token in the URL picks the account; the request must be signed with
    that account's webhook signing secret (webhook_security.py), so knowing
    an email and a row reference is no longer enough to change a task.
    Body (0.10.0 — any subset of the fields; the most recent edit wins):
    {"external_ref": "row-2", "task": "...", "owner": "Kevin", "owner_email": "kevin@x.com",
     "due_date": "2026-10-31", "status": "Done", "edited_at": 1760500000, "provider": "google_sheets"}"""
    if not webhook_security.limiter.allow(f"tracker:{token}"):
        return jsonify({"error": "too many requests — slow down"}), 429
    db, email = _webhook_account(token)
    if email is None:
        db.close()
        return jsonify({"error": "unrecognized webhook token"}), 404
    ok, reason, delivery_id = webhook_security.verify_request(db, email, "tracker", request.headers, request.get_data())
    if not ok:
        db.close()
        return jsonify({"error": reason}), 401
    data = request.get_json(force=True, silent=True) or {}
    result, failure = _apply_tracker_payload(db, email, data, sent_at=request.headers.get("X-CT-Timestamp"))
    if failure:
        webhook_security.record(db, email, "tracker", delivery_id, "rejected", failure[0]["error"])
        db.commit()
        db.close()
        return jsonify(failure[0]), failure[1]
    webhook_security.remember_delivery(db, email, "tracker", delivery_id, result["detail"])
    db.commit()
    db.close()
    notify(email, "commitment_status", commitment_id=result["commitment_id"], detail=result["detail"])
    return jsonify({"ok": True, **result}), 201


@app.route("/api/webhooks/tracker-update", methods=["POST"])
def legacy_tracker_webhook():
    """The pre-0.9.0 unsigned endpoint, identified only by email +
    external_ref in the body. Off unless CT_ALLOW_UNSIGNED_TRACKER_WEBHOOK=1
    (a temporary bridge while an existing automation is updated)."""
    if not legacy_tracker_webhook_enabled():
        return jsonify({"error": "this unsigned webhook URL was retired in 0.9.0 — use the signed URL from "
                                 "Integrations → Webhook security"}), 410
    data = request.get_json(force=True) or {}
    email = (data.get("email") or "").strip().lower()
    if not email:
        return jsonify({"error": "email and external_ref are required"}), 400
    db = get_db()
    result, failure = _apply_tracker_payload(db, email, data)
    if failure:
        db.close()
        return jsonify(failure[0]), failure[1]
    db.commit()
    db.close()
    notify(email, "commitment_status", commitment_id=result["commitment_id"], detail=result["detail"])
    return jsonify({"ok": True, **result}), 201


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
        return jsonify({"provider": "none", "custom_name": None, "config": {}, "secret_hints": {}, "enabled": True,
                        "webhook_token": token})
    db.close()
    unreadable = None
    try:
        config = secret_store.load_config(row["config_json"])
    except secret_store.SecretUnreadable as e:
        config, unreadable = {}, str(e)
    config, hints = _hide_secrets(config)
    return jsonify({
        "provider": row["provider"],
        "custom_name": row["custom_name"],
        "config": config,
        "secret_hints": hints,
        "enabled": bool(row["enabled"]),
        "webhook_token": row["webhook_token"],
        "unreadable": unreadable,
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

    db = get_db()
    previous_provider, previous = _stored_settings(db, "transcript_source_settings", email)
    if previous_provider == provider:
        _keep_blank_secrets(config, previous)
    if provider != "none":
        missing = [f for f in TRANSCRIPT_SOURCE_REQUIRED_FIELDS[provider] if not (config.get(f) or "").strip()]
        if missing:
            db.close()
            return jsonify({"error": f"missing required field(s) for {provider}: {', '.join(missing)}"}), 400

    token = get_or_create_webhook_token(db, email)
    db.execute(
        "INSERT INTO transcript_source_settings (user_email, provider, custom_name, config_json, enabled, webhook_token) "
        "VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(user_email) DO UPDATE SET "
        "provider = excluded.provider, custom_name = excluded.custom_name, "
        "config_json = excluded.config_json, enabled = excluded.enabled",
        (email, provider, custom_name, secret_store.dump_config(config), enabled, token),
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
    config = secret_store.load_config(source["config_json"])
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
    so the user is identified via the webhook_token in the URL, and the
    request must be signed with the account's webhook signing secret
    (X-CT-Timestamp / X-CT-Signature, see webhook_security.py). Extraction
    runs as a background job, so the sender gets its reply immediately."""
    if not webhook_security.limiter.allow(f"transcript:{token}"):
        return jsonify({"error": "too many requests — slow down"}), 429
    db = get_db()
    source = db.execute(
        "SELECT user_email, provider FROM transcript_source_settings WHERE webhook_token = ?", (token,)
    ).fetchone()
    if source is None:
        db.close()
        return jsonify({"error": "unrecognized webhook token"}), 404
    email = source["user_email"]
    ok, reason, delivery_id = webhook_security.verify_request(db, email, "transcript", request.headers, request.get_data())
    if not ok:
        db.close()
        return jsonify({"error": reason}), 401

    data = request.get_json(force=True, silent=True) or {}
    title = (data.get("title") or "").strip()
    meeting_date = (data.get("meeting_date") or "").strip() or today_str()
    transcript_excerpt = (data.get("transcript_excerpt") or "").strip() or None
    provider = (data.get("provider") or "").strip() or source["provider"] or "custom"

    problem = None
    if not title:
        problem = "title is required"
    elif transcript_excerpt and looks_like_binary_text(transcript_excerpt):
        problem = "transcript_excerpt looks like a binary file, not plain text"
    if problem:
        webhook_security.record(db, email, "transcript", delivery_id, "rejected", problem)
        db.commit()
        db.close()
        return jsonify({"error": problem}), 400

    meeting_id = create_meeting_row(db, email, title, meeting_date, transcript_excerpt, source="live", transcript_provider=provider)
    log_inbound_capture(db, email, provider, meeting_id, f'Received live transcript for "{title}"')
    webhook_security.remember_delivery(db, email, "transcript", delivery_id, f'meeting "{title}"')
    notify(email, "meeting_created", meeting_id=meeting_id, source="live", provider=provider)
    outcome = queue_extraction(db, email, meeting_id) if transcript_excerpt else {"job_id": None, "extraction": None}
    if not transcript_excerpt:
        db.commit()
    db.close()
    return jsonify({"ok": True, "meeting_id": meeting_id, **outcome}), 201


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
    session cookie. Since 0.9.0 the signed timestamp must also be within 5
    minutes and each webhook-id is accepted once (replay protection), and
    extraction runs as a background job so Fathom gets a fast reply."""
    if not webhook_security.limiter.allow(f"fathom:{token}"):
        return jsonify({"error": "too many requests — slow down"}), 429
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
    email = source["user_email"]
    delivery_id = (request.headers.get("webhook-id") or "")[:200]

    config = secret_store.load_config(source["config_json"], default_on_error=True)
    reason = None
    if not verify_fathom_signature(config.get("webhook_secret", ""), request):
        reason = "signature verification failed"
    else:
        reason = webhook_security.check_timestamp(request.headers.get("webhook-timestamp"))
        if reason is None and webhook_security.seen_before(db, "fathom", delivery_id):
            reason = "this delivery was already received (replay)"
    if reason:
        webhook_security.record(db, email, "fathom", delivery_id, "rejected", reason)
        db.commit()
        db.close()
        return jsonify({"error": reason}), 401

    payload = request.get_json(force=True)
    meeting, turns = _fathom_speaker_turns(payload)
    title = meeting.get("title") or meeting.get("meeting_title") or "Fathom meeting"
    raw_date = meeting.get("scheduled_start_time") or meeting.get("recording_start_time") or meeting.get("created_at")
    meeting_date = (raw_date or "")[:10] or today_str()
    transcript_excerpt = "\n\n".join(f"{t['speaker']}: {t['text']}" for t in turns) or None

    meeting_id = create_meeting_row(db, email, title, meeting_date, transcript_excerpt, source="live", transcript_provider="fathom")
    log_inbound_capture(db, email, "fathom", meeting_id, f'Received Fathom transcript for "{title}" ({len(turns)} turns)')
    webhook_security.remember_delivery(db, email, "fathom", delivery_id, f'meeting "{title}" ({len(turns)} turns)')
    notify(email, "meeting_created", meeting_id=meeting_id, source="live", provider="fathom")
    outcome = queue_extraction(db, email, meeting_id, turns=turns) if turns else {"job_id": None, "extraction": None}
    if not turns:
        db.commit()
    db.close()
    return jsonify({"ok": True, "meeting_id": meeting_id, **outcome}), 201


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
    notify(email, "meeting_created", meeting_id=meeting_id, source="live", provider=provider)
    outcome = queue_extraction(db, email, meeting_id)
    db.close()
    return jsonify({"ok": True, "meeting_id": meeting_id, **outcome}), 201


# ---------- webhook security settings (Integrations page) ----------

@app.route("/api/webhook-security", methods=["GET"])
def webhook_security_settings():
    """The account's webhook URLs, a masked signing secret, and recent deliveries."""
    email = require_email()
    if not email:
        return jsonify({"error": "not identified"}), 401
    db = get_db()
    token = get_or_create_webhook_token(db, email)
    try:
        secret_hint = _mask(webhook_security.get_secret(db, email))
        unreadable = None
    except secret_store.SecretUnreadable as e:
        secret_hint, unreadable = "", str(e)
    deliveries = webhook_security.recent_deliveries(db, email)
    db.commit()
    db.close()
    return jsonify({
        "webhook_token": token,
        "secret_hint": secret_hint,
        "unreadable": unreadable,
        "tolerance_seconds": webhook_security.TOLERANCE_SECONDS,
        "rate_limit_per_minute": webhook_security.RATE_LIMIT,
        "paths": {
            "tracker": f"/api/webhooks/tracker-update/{token}",
            "transcript": f"/api/webhooks/transcript-capture/{token}",
            "fathom": f"/api/webhooks/fathom/{token}",
        },
        "legacy_tracker_webhook_enabled": legacy_tracker_webhook_enabled(),
        "deliveries": deliveries,
    })


@app.route("/api/webhook-security/reveal", methods=["POST"])
def reveal_webhook_secret():
    email = require_email()
    if not email:
        return jsonify({"error": "not identified"}), 401
    db = get_db()
    secret = webhook_security.get_secret(db, email)
    db.commit()
    db.close()
    return jsonify({"secret": secret})


@app.route("/api/webhook-security/rotate", methods=["POST"])
def rotate_webhook_secret():
    """New signing secret; requests signed with the old one stop working at once."""
    email = require_email()
    if not email:
        return jsonify({"error": "not identified"}), 401
    db = get_db()
    secret = webhook_security.rotate_secret(db, email)
    db.commit()
    db.close()
    return jsonify({"ok": True, "secret": secret}), 201


# ---------- background jobs (Jobs page) ----------

@app.route("/api/jobs", methods=["GET"])
def list_background_jobs():
    email = require_email()
    if not email:
        return jsonify({"error": "not identified"}), 401
    db = get_db()
    result = {"jobs": jobs.list_jobs(db, email), "counts": jobs.counts(db, email), "inline": jobs.inline_mode()}
    db.close()
    return jsonify(result)


@app.route("/api/jobs/<int:job_id>/<action>", methods=["POST"])
def change_background_job(job_id, action):
    """retry: a failed or cancelled job goes back in the queue (one more attempt);
    cancel: a queued job is dropped."""
    email = require_email()
    if not email:
        return jsonify({"error": "not identified"}), 401
    if action not in ("retry", "cancel"):
        return jsonify({"error": "action must be retry or cancel"}), 400
    db = get_db()
    changed = jobs.retry(db, email, job_id) if action == "retry" else jobs.cancel(db, email, job_id)
    db.commit()
    db.close()
    if not changed:
        return jsonify({"error": f"that job can't be {'retried' if action == 'retry' else 'cancelled'} now"}), 409
    job = jobs.after_commit(job_id) if action == "retry" else jobs.get_job(job_id)
    notify(email, "job_updated", job_id=job_id, status=(job or {}).get("status", "queued"))
    return jsonify({"ok": True, "job": job})


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


def warn_missing_packages():
    """Starting the app with a Python that lacks requirements.txt (e.g. the
    system Python instead of the project's .venv) breaks extraction, PDF
    upload and Google Sheets in ways that only show up later — say so now."""
    import importlib.util
    needed = {"googleapiclient": "Google Sheets sync", "google_auth_oauthlib": "Google Sheets sync",
              "pypdf": ".pdf upload", "tensorflow": "built-in extraction",
              "sentence_transformers": "built-in extraction", "anthropic": "Claude AI engine"}
    missing = [f"{mod} ({use})" for mod, use in needed.items() if importlib.util.find_spec(mod) is None]
    if missing:
        import sys
        print(f"[setup] WARNING: this Python ({sys.executable}) is missing: {', '.join(missing)}.\n"
              "[setup] Start the app with the project's virtual environment instead, e.g.\n"
              "[setup]     ..\\.venv\\Scripts\\python src\\app.py\n"
              "[setup] or install them here: pip install -r requirements.txt")


if __name__ == "__main__":
    warn_missing_packages()
    if not DB_PATH.exists():
        init_db()
        print(f"Initialized new database at {DB_PATH}")
    migrate_db()
    # The Werkzeug debugger runs arbitrary code from the browser, so it's off
    # unless asked for (CT_DEBUG=1, and only ever on localhost).
    debug = os.environ.get("CT_DEBUG") == "1"
    # With the debug reloader, this block runs in a watcher process and in the
    # process that actually serves requests; only start the worker in the latter.
    if not debug or os.environ.get("WERKZEUG_RUN_MAIN") == "true":
        start_background_services()
    app.run(debug=debug, port=int(os.environ.get("PORT", "5000")), threaded=True)
