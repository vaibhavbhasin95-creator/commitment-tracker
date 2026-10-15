"""
Webhook security (0.9.0): signatures, replay protection and rate limits for
the inbound webhooks (/api/webhooks/...).

Every account has one signing secret (table webhook_secrets, encrypted at
rest with secret_store). A sender proves it knows the secret by signing each
request:

    X-CT-Timestamp: <unix seconds>
    X-CT-Signature: v1=<hex HMAC-SHA256 of "<timestamp>.<raw request body>">
    X-CT-Delivery:  <optional unique id for this delivery>

verify_request() then checks, in order:
  1. both headers are present and the signature matches (constant-time);
  2. the timestamp is within TOLERANCE_SECONDS of now, so a captured request
     can't be replayed later;
  3. this delivery (X-CT-Delivery, or the signature itself) hasn't been seen
     before — a replay inside the tolerance window is rejected too.

Fathom signs with its own (Svix-style) scheme; app.py verifies that
signature and uses check_timestamp() + remember_delivery() here for the same
replay protection.

Every accepted or rejected delivery is recorded in webhook_deliveries (shown
on the Integrations page), and rows older than KEEP_DAYS are pruned.

Rate limiting is per webhook URL (token), in memory, RATE_LIMIT requests per
RATE_WINDOW seconds — enough for any real automation, low enough to blunt
brute-force or flooding.
"""

import hashlib
import hmac
import secrets
import threading
import time
from datetime import datetime, timedelta

import secret_store

TOLERANCE_SECONDS = 300
KEEP_DAYS = 7
RATE_LIMIT = 60
RATE_WINDOW = 60
SECRET_PREFIX = "ctwhsec_"

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS webhook_secrets (
    user_email TEXT PRIMARY KEY,
    secret TEXT NOT NULL,              -- encrypted (secret_store)
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS webhook_deliveries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_email TEXT,
    endpoint TEXT NOT NULL,            -- 'tracker' | 'transcript' | 'fathom'
    delivery_id TEXT,
    received_at TEXT NOT NULL,
    status TEXT NOT NULL,              -- 'accepted' | 'rejected'
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_webhook_deliveries_lookup ON webhook_deliveries(endpoint, delivery_id);
"""


def _now():
    return datetime.utcnow().isoformat(timespec="seconds")


def ensure_schema(db):
    db.executescript(SCHEMA_SQL)


def new_secret():
    return SECRET_PREFIX + secrets.token_urlsafe(32)


def get_secret(db, email, create=True):
    """The account's signing secret (plain text), created on first use."""
    ensure_schema(db)
    row = db.execute("SELECT secret FROM webhook_secrets WHERE user_email = ?", (email,)).fetchone()
    if row:
        return secret_store.decrypt(row["secret"])
    if not create:
        return None
    value = new_secret()
    db.execute("INSERT INTO webhook_secrets (user_email, secret, created_at) VALUES (?, ?, ?)",
               (email, secret_store.encrypt(value), _now()))
    return value


def rotate_secret(db, email):
    ensure_schema(db)
    value = new_secret()
    db.execute(
        "INSERT INTO webhook_secrets (user_email, secret, created_at) VALUES (?, ?, ?) "
        "ON CONFLICT(user_email) DO UPDATE SET secret = excluded.secret, created_at = excluded.created_at",
        (email, secret_store.encrypt(value), _now()),
    )
    return value


def sign(secret, timestamp, body):
    """The X-CT-Signature value for a body (bytes or str) — what a sender computes."""
    if isinstance(body, str):
        body = body.encode("utf-8")
    mac = hmac.new(secret.encode("utf-8"), str(timestamp).encode("ascii") + b"." + body, hashlib.sha256)
    return "v1=" + mac.hexdigest()


def check_timestamp(raw, now=None):
    """None if the timestamp is fresh, else the reason it's rejected."""
    now = now or time.time()
    try:
        ts = int(str(raw).strip())
    except (TypeError, ValueError):
        return "missing or invalid timestamp header"
    if ts > 10_000_000_000:          # milliseconds — accept those too
        ts //= 1000
    if abs(now - ts) > TOLERANCE_SECONDS:
        return f"timestamp is more than {TOLERANCE_SECONDS // 60} minutes from the server's clock (old or replayed request?)"
    return None


def seen_before(db, endpoint, delivery_id):
    ensure_schema(db)
    return db.execute(
        "SELECT 1 FROM webhook_deliveries WHERE endpoint = ? AND delivery_id = ? AND status = 'accepted' LIMIT 1",
        (endpoint, delivery_id),
    ).fetchone() is not None


def record(db, email, endpoint, delivery_id, status, detail):
    """Logs a delivery and prunes old ones. The caller commits."""
    ensure_schema(db)
    db.execute(
        "INSERT INTO webhook_deliveries (user_email, endpoint, delivery_id, received_at, status, detail) VALUES (?, ?, ?, ?, ?, ?)",
        (email, endpoint, delivery_id, _now(), status, (detail or "")[:300]),
    )
    cutoff = (datetime.utcnow() - timedelta(days=KEEP_DAYS)).isoformat(timespec="seconds")
    db.execute("DELETE FROM webhook_deliveries WHERE received_at < ?", (cutoff,))


def verify_request(db, email, endpoint, headers, raw_body, now=None):
    """Checks an X-CT-signed request for this account. Returns
    (ok, reason, delivery_id). Records rejections; the caller records the
    acceptance (with remember_delivery) once it has processed the request."""
    secret = get_secret(db, email)
    signature = (headers.get("X-CT-Signature") or "").strip()
    timestamp = headers.get("X-CT-Timestamp")
    delivery_id = (headers.get("X-CT-Delivery") or "").strip()[:200] or signature
    reason = None
    if not signature or not timestamp:
        reason = "unsigned request — add the X-CT-Timestamp and X-CT-Signature headers (see Integrations → Webhook security)"
    elif not hmac.compare_digest(signature, sign(secret, str(timestamp).strip(), raw_body)):
        reason = "signature doesn't match — check the signing secret, and sign '<timestamp>.<exact body>'"
    else:
        reason = check_timestamp(timestamp, now)
        if reason is None and seen_before(db, endpoint, delivery_id):
            reason = "this delivery was already received (replay)"
    if reason:
        record(db, email, endpoint, delivery_id, "rejected", reason)
        db.commit()
        return False, reason, delivery_id
    return True, None, delivery_id


def remember_delivery(db, email, endpoint, delivery_id, detail):
    record(db, email, endpoint, delivery_id, "accepted", detail)


def recent_deliveries(db, email, limit=15):
    ensure_schema(db)
    return [dict(r) for r in db.execute(
        "SELECT endpoint, delivery_id, received_at, status, detail FROM webhook_deliveries "
        "WHERE user_email = ? ORDER BY id DESC LIMIT ?", (email, limit)).fetchall()]


class RateLimiter:
    """Sliding-window counter per key (in memory, one process)."""

    def __init__(self, limit=RATE_LIMIT, window=RATE_WINDOW):
        self.limit, self.window = limit, window
        self._lock = threading.Lock()
        self._hits = {}

    def allow(self, key, now=None):
        now = now or time.time()
        with self._lock:
            hits = [t for t in self._hits.get(key, []) if now - t < self.window]
            if len(hits) >= self.limit:
                self._hits[key] = hits
                return False
            hits.append(now)
            self._hits[key] = hits
            return True


limiter = RateLimiter()
