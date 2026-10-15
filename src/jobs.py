"""
Background jobs (0.9.0): a small persistent job queue in the app's SQLite
database, worked by one thread inside the app process.

Why: commitment extraction can take a minute with an AI engine, a webhook
sender (Fathom) expects a fast reply, and a Google Sheets or email outage
shouldn't lose work. So slow or failure-prone work is queued instead of run
inside the request:

    job_id = jobs.enqueue(db, email, "extract_commitments", {...}, meeting_id=...)
    db.commit()                      # the job becomes visible to the worker
    result = jobs.after_commit(job_id)   # None normally; the result when inline

Lifecycle: queued → running → succeeded | failed (| cancelled). A handler
that raises is retried with exponential backoff (RETRY_BASE_SECONDS × 2^n) up
to max_attempts; raising PermanentError fails it straight away (e.g. "the
model isn't trained" — retrying won't help). Jobs survive a restart: anything
left 'running' by a stopped process is put back in the queue on start-up.

Periodic tasks (add_periodic) run on the same worker thread — the automatic
nudge check is one; it enqueues one 'run_due_nudges' job per account.

CT_JOBS_INLINE=1 runs each job synchronously in after_commit() instead of on
the worker (the test suites use this, so responses carry the result). Like
live_events, this is one process: several worker processes would need a
shared queue (Redis, Postgres SKIP LOCKED, ...) instead.
"""

import json
import os
import threading
import time
import traceback
from datetime import datetime, timedelta

POLL_SECONDS = 2.0
RETRY_BASE_SECONDS = 30
STALE_AFTER_SECONDS = 15 * 60
KEEP_FINISHED_DAYS = 14

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_email TEXT,
    kind TEXT NOT NULL,
    meeting_id INTEGER,                    -- set for meeting-related jobs, for the dashboard's status badge
    payload_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'queued', -- 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled'
    attempts INTEGER NOT NULL DEFAULT 0,
    max_attempts INTEGER NOT NULL DEFAULT 3,
    run_after TEXT NOT NULL,
    last_error TEXT,
    result_json TEXT,
    created_at TEXT NOT NULL,
    started_at TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_queue ON jobs(status, run_after);
CREATE INDEX IF NOT EXISTS idx_jobs_user ON jobs(user_email, id);
"""

KIND_LABELS = {}
_handlers = {}
_periodic = []          # [name, interval_seconds, fn, next_due]
_wake = threading.Event()
_started = False
_start_lock = threading.Lock()
_get_db = None
_on_change = None


class PermanentError(Exception):
    """Raised by a handler when retrying can't help; the job fails at once."""


def _now():
    return datetime.utcnow().isoformat(timespec="seconds")


def inline_mode():
    return os.environ.get("CT_JOBS_INLINE") == "1"


def init(get_db, on_change=None):
    """get_db() -> sqlite3 connection; on_change(user_email, job_dict) is
    called whenever a job changes state (used for live dashboard updates)."""
    global _get_db, _on_change
    _get_db, _on_change = get_db, on_change


def ensure_schema(db):
    db.executescript(SCHEMA_SQL)


def handler(kind, label):
    """Decorator: @jobs.handler("extract_commitments", "Extract commitments")
    def fn(payload, job) -> dict (stored as the job's result)."""
    def register(fn):
        _handlers[kind] = fn
        KIND_LABELS[kind] = label
        return fn
    return register


def add_periodic(name, interval_seconds, fn, first_delay=None):
    _periodic.append([name, interval_seconds, fn, time.time() + (interval_seconds if first_delay is None else first_delay)])


# ---------------------------------------------------------------------------
# Queue operations
# ---------------------------------------------------------------------------

def enqueue(db, user_email, kind, payload=None, meeting_id=None, max_attempts=3, delay_seconds=0):
    """Adds a job inside the caller's transaction (visible once it commits)."""
    if kind not in _handlers:
        raise ValueError(f"no handler registered for job kind '{kind}'")
    ensure_schema(db)
    run_after = (datetime.utcnow() + timedelta(seconds=delay_seconds)).isoformat(timespec="seconds")
    cur = db.execute(
        "INSERT INTO jobs (user_email, kind, meeting_id, payload_json, max_attempts, run_after, created_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (user_email, kind, meeting_id, json.dumps(payload or {}), max_attempts, run_after, _now()),
    )
    return cur.lastrowid


def after_commit(job_id):
    """Call after committing the enqueue. Inline mode: runs the job now and
    returns its row as a dict. Otherwise: wakes the worker, returns None."""
    if inline_mode():
        if _claim(job_id):
            _run(job_id)
        return get_job(job_id)
    _wake.set()
    return None


def _claim(job_id=None):
    """Marks one due queued job (or the given one) as running. Returns its id or None."""
    db = _get_db()
    try:
        db.execute("BEGIN IMMEDIATE")
        if job_id is None:
            row = db.execute(
                "SELECT id FROM jobs WHERE status = 'queued' AND run_after <= ? ORDER BY id LIMIT 1", (_now(),)
            ).fetchone()
            if row is None:
                db.rollback()
                return None
            job_id = row["id"]
        cur = db.execute(
            "UPDATE jobs SET status = 'running', attempts = attempts + 1, started_at = ? WHERE id = ? AND status = 'queued'",
            (_now(), job_id),
        )
        db.commit()
        return job_id if cur.rowcount else None
    finally:
        db.close()


def _run(job_id):
    db = _get_db()
    job = db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    db.close()
    fn = _handlers.get(job["kind"])
    try:
        if fn is None:
            raise PermanentError(f"unknown job kind '{job['kind']}'")
        result = fn(json.loads(job["payload_json"] or "{}"), dict(job)) or {}
        status, error, run_after = "succeeded", None, None
    except PermanentError as e:
        result, status, error, run_after = None, "failed", str(e), None
    except Exception as e:  # retry with backoff
        result, error = None, f"{type(e).__name__}: {e}"
        if job["attempts"] < job["max_attempts"]:
            delay = RETRY_BASE_SECONDS * (2 ** (job["attempts"] - 1))
            status = "queued"
            run_after = (datetime.utcnow() + timedelta(seconds=delay)).isoformat(timespec="seconds")
            error += f" — retrying in {delay}s (attempt {job['attempts']} of {job['max_attempts']})"
        else:
            status, run_after = "failed", None
        if os.environ.get("CT_JOBS_TRACEBACKS") == "1":
            traceback.print_exc()

    db = _get_db()
    db.execute(
        "UPDATE jobs SET status = ?, last_error = ?, result_json = ?, run_after = COALESCE(?, run_after), "
        "finished_at = CASE WHEN ? IN ('succeeded', 'failed') THEN ? ELSE finished_at END WHERE id = ?",
        (status, error, json.dumps(result) if result is not None else None, run_after, status, _now(), job_id),
    )
    db.commit()
    db.close()
    _changed(job_id)


def _changed(job_id):
    if _on_change is None:
        return
    job = get_job(job_id)
    if job:
        try:
            _on_change(job["user_email"], job)
        except Exception as e:
            print(f"[jobs] change callback failed: {e}")


def run_pending(max_jobs=50):
    """Works through due jobs. Returns how many ran."""
    ran = 0
    while ran < max_jobs:
        job_id = _claim()
        if job_id is None:
            break
        _run(job_id)
        ran += 1
    return ran


def run_periodic(now=None):
    now = now or time.time()
    for task in _periodic:
        name, interval, fn, due = task
        if now >= due:
            task[3] = now + interval
            try:
                fn()
            except Exception as e:
                print(f"[jobs] periodic task '{name}' failed: {e}")


def recover_stale():
    """Puts jobs a previous process left 'running' back in the queue, and
    prunes finished jobs older than KEEP_FINISHED_DAYS."""
    db = _get_db()
    ensure_schema(db)
    cutoff = (datetime.utcnow() - timedelta(days=KEEP_FINISHED_DAYS)).isoformat(timespec="seconds")
    db.execute("DELETE FROM jobs WHERE status IN ('succeeded', 'failed', 'cancelled') AND finished_at < ?", (cutoff,))
    cur = db.execute("UPDATE jobs SET status = 'queued', last_error = 'restarted: the app stopped while this was running' "
                     "WHERE status = 'running'")
    db.commit()
    db.close()
    return cur.rowcount


def _worker_loop():
    while True:
        _wake.wait(POLL_SECONDS)
        _wake.clear()
        try:
            run_periodic()
            run_pending()
        except Exception as e:  # never let the worker die
            print(f"[jobs] worker pass failed: {e}")


def start():
    """Starts the worker thread once per process. No-op in inline mode."""
    global _started
    with _start_lock:
        if _started or inline_mode():
            return False
        _started = True
    recovered = recover_stale()
    threading.Thread(target=_worker_loop, daemon=True, name="job-worker").start()
    print(f"[jobs] background worker started" + (f" — re-queued {recovered} interrupted job(s)" if recovered else ""))
    return True


# ---------------------------------------------------------------------------
# Reading / managing jobs (for the Jobs page)
# ---------------------------------------------------------------------------

def _public(row):
    job = dict(row)
    job["payload"] = json.loads(job.pop("payload_json") or "{}")
    job["payload"].pop("turns", None)  # can be large; not useful in a list
    result_json = job.pop("result_json", None)
    job["result"] = json.loads(result_json) if result_json else None
    job["label"] = KIND_LABELS.get(job["kind"], job["kind"])
    return job


def get_job(job_id):
    db = _get_db()
    row = db.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    db.close()
    return _public(row) if row else None


def list_jobs(db, user_email, limit=50):
    ensure_schema(db)
    return [_public(r) for r in db.execute(
        "SELECT * FROM jobs WHERE user_email = ? ORDER BY id DESC LIMIT ?", (user_email, limit)).fetchall()]


def counts(db, user_email):
    ensure_schema(db)
    return {r["status"]: r["n"] for r in db.execute(
        "SELECT status, COUNT(*) AS n FROM jobs WHERE user_email = ? GROUP BY status", (user_email,)).fetchall()}


def retry(db, user_email, job_id):
    cur = db.execute(
        "UPDATE jobs SET status = 'queued', run_after = ?, last_error = NULL, finished_at = NULL, "
        "max_attempts = attempts + 1 WHERE id = ? AND user_email = ? AND status IN ('failed', 'cancelled')",
        (_now(), job_id, user_email),
    )
    return cur.rowcount > 0


def cancel(db, user_email, job_id):
    cur = db.execute(
        "UPDATE jobs SET status = 'cancelled', finished_at = ? WHERE id = ? AND user_email = ? AND status = 'queued'",
        (_now(), job_id, user_email),
    )
    return cur.rowcount > 0


def cancel_for_meetings(db, meeting_ids):
    if not meeting_ids:
        return
    ensure_schema(db)
    marks = ",".join("?" * len(meeting_ids))
    db.execute(f"UPDATE jobs SET status = 'cancelled', finished_at = ? WHERE status = 'queued' AND meeting_id IN ({marks})",
               (_now(), *meeting_ids))


def meeting_extraction_status(db, user_email):
    """{meeting_id: {"status", "error"}} from each meeting's latest extraction job."""
    ensure_schema(db)
    rows = db.execute(
        "SELECT j.meeting_id, j.status, j.last_error FROM jobs j JOIN ("
        "  SELECT meeting_id, MAX(id) AS id FROM jobs WHERE user_email = ? AND kind = 'extract_commitments' "
        "  AND meeting_id IS NOT NULL GROUP BY meeting_id) latest ON latest.id = j.id",
        (user_email,),
    ).fetchall()
    return {r["meeting_id"]: {"status": r["status"], "error": r["last_error"]} for r in rows}
