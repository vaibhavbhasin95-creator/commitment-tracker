"""
Real authentication (0.9.0) — replaces the old "type your email" identify step.

Two ways to sign in, both ending in the same signed session cookie:

  * Email + password. Passwords are hashed with Werkzeug's scrypt/pbkdf2
    (never stored or logged in plain text). Repeated failures for an email or
    an IP address are locked out for a while (LOGIN_LOCKOUT_*).
  * Single sign-on (OpenID Connect) with any standard provider — Google,
    Microsoft Entra ID, Okta, Auth0, Keycloak... — configured in
    auth_config.json (project root) or CT_SSO_* environment variables; see
    load_sso_providers(). Authorization-code flow with PKCE, state and nonce.

Sessions:
  * The Flask secret key comes from COMMITMENT_TRACKER_FLASK_SECRET or the
    file flask_secret.key in the project root (generated on first run) — no
    longer hard-coded in source.
  * Cookies are HttpOnly and SameSite=Lax; set CT_COOKIE_SECURE=1 when the
    app is served over HTTPS (e.g. behind ngrok) to make them Secure too.
  * Each user has a session_version. Changing the password or "sign out
    everywhere" bumps it, which invalidates every other existing session.

CSRF: every state-changing /api/ request from the browser must carry the
header X-CT-Request: 1 (static/app.js adds it). A cross-site form can't set
custom headers, so it can't forge requests even if a cookie were sent.
Webhooks (/api/webhooks/...) are exempt — they authenticate with their own
token + signature (webhook_security.py), not the session.

Existing accounts from before 0.9.0 have no password. Their owner can either
sign in with SSO (a verified email from the identity provider proves they own
the address, and the account is linked) or set a password from the machine
the app runs on:

    python src/manage_users.py set-password you@example.com
"""

import base64
import hashlib
import json
import logging
import os
import secrets
import sqlite3
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

from flask import Blueprint, jsonify, redirect, request, session
from werkzeug.security import check_password_hash, generate_password_hash

PROJECT_ROOT = Path(__file__).resolve().parent.parent
FLASK_SECRET_PATH = PROJECT_ROOT / "flask_secret.key"
AUTH_CONFIG_PATH = PROJECT_ROOT / "auth_config.json"

MIN_PASSWORD_LENGTH = 10
# scrypt at OWASP's minimum (N=2^17, r=8, p=1; ~128 MB per hash). Werkzeug's
# default is N=2^15; older hashes are upgraded at the next successful sign-in.
PASSWORD_HASH_METHOD = "scrypt:131072:8:1"
LOGIN_LOCKOUT_FAILURES = 5          # failures within the window before locking
LOGIN_LOCKOUT_WINDOW = 15 * 60      # seconds
SESSION_LIFETIME = timedelta(days=14)
SSO_STATE_TTL = 10 * 60             # seconds a started SSO login stays valid
HTTP_TIMEOUT = 15

CSRF_HEADER = "X-CT-Request"
CSRF_EXEMPT_PREFIXES = ("/api/webhooks/",)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS auth_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    event TEXT NOT NULL,             -- e.g. 'login', 'login_failed', 'password_changed'
    email TEXT,
    ip TEXT,
    detail TEXT
);
CREATE TABLE IF NOT EXISTS user_identities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_email TEXT NOT NULL,
    provider TEXT NOT NULL,          -- SSO provider id from the config, e.g. 'google'
    subject TEXT NOT NULL,           -- the provider's stable user id ('sub' claim)
    created_at TEXT NOT NULL,
    last_used_at TEXT,
    UNIQUE(provider, subject)
);
"""

# Columns added to `users` (migrate()).
USER_COLUMNS = {
    "password_hash": "TEXT",
    "display_name": "TEXT",
    "session_version": "INTEGER NOT NULL DEFAULT 1",
    "last_login_at": "TEXT",
}

# Defaults for well-known providers; anything else needs an explicit issuer.
SSO_PRESETS = {
    "google": {"label": "Google", "issuer": "https://accounts.google.com"},
    "microsoft": {"label": "Microsoft", "issuer": "https://login.microsoftonline.com/{tenant}/v2.0", "tenant": "organizations"},
    "okta": {"label": "Okta"},
    "auth0": {"label": "Auth0"},
    "keycloak": {"label": "Keycloak"},
}


class AuthError(Exception):
    """A user-facing sign-in problem."""


def _now():
    return datetime.utcnow().isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Secret key + cookie settings
# ---------------------------------------------------------------------------

def load_flask_secret():
    env = os.environ.get("COMMITMENT_TRACKER_FLASK_SECRET", "").strip()
    if env:
        return env
    if FLASK_SECRET_PATH.exists():
        return FLASK_SECRET_PATH.read_text().strip()
    key = secrets.token_urlsafe(48)
    FLASK_SECRET_PATH.write_text(key)
    print(f"[auth] generated a new session signing key at {FLASK_SECRET_PATH}")
    return key


def configure_app(app):
    app.secret_key = load_flask_secret()
    app.config.update(
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        SESSION_COOKIE_SECURE=os.environ.get("CT_COOKIE_SECURE") == "1",
        SESSION_COOKIE_NAME="ct_session",
        PERMANENT_SESSION_LIFETIME=SESSION_LIFETIME,
    )


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------

def migrate(conn):
    """Adds the auth columns/tables to an existing database. Idempotent."""
    conn.executescript(SCHEMA_SQL)
    cols = {row[1] for row in conn.execute("PRAGMA table_info(users)")}
    for name, decl in USER_COLUMNS.items():
        if name not in cols:
            conn.execute(f"ALTER TABLE users ADD COLUMN {name} {decl}")


# ---------------------------------------------------------------------------
# Passwords + lockout
# ---------------------------------------------------------------------------

def password_problem(password):
    """None if acceptable, else a user-facing reason."""
    if len(password or "") < MIN_PASSWORD_LENGTH:
        return f"use at least {MIN_PASSWORD_LENGTH} characters"
    if password.strip() != password:
        return "the password can't start or end with a space"
    if len(set(password)) < 4:
        return "that password is too repetitive"
    return None


def hash_password(password):
    return generate_password_hash(password, method=PASSWORD_HASH_METHOD)


def needs_rehash(stored):
    """True for a hash made with weaker settings than PASSWORD_HASH_METHOD."""
    return bool(stored) and not stored.startswith(PASSWORD_HASH_METHOD + "$")


# ---------------------------------------------------------------------------
# Audit trail
# ---------------------------------------------------------------------------

audit_logger = logging.getLogger("commitment_tracker.auth")


def audit(db, event, email=None, detail=None):
    """Records a sign-in / account event in auth_events and the app log. Never
    stores passwords or tokens. Inside a request the caller's IP is included."""
    try:
        ip = request.remote_addr or "?"
    except RuntimeError:  # outside a request (e.g. manage_users.py)
        ip = None
    audit_logger.info("auth event=%s email=%s ip=%s %s", event, email, ip, detail or "")
    try:
        db.execute("INSERT INTO auth_events (created_at, event, email, ip, detail) VALUES (?, ?, ?, ?, ?)",
                   (_now(), event, email, ip, (detail or "")[:300] or None))
        db.commit()
    except sqlite3.Error as e:  # auditing must never break sign-in
        audit_logger.warning("couldn't record auth event %s: %s", event, e)


class LoginThrottle:
    """In-memory failure counter per email and per IP. Resets on success or
    after LOGIN_LOCKOUT_WINDOW. One process only, like live_events."""

    def __init__(self):
        self._lock = threading.Lock()
        self._fails = {}   # key -> list of failure timestamps

    def _recent(self, key, now):
        stamps = [t for t in self._fails.get(key, []) if now - t < LOGIN_LOCKOUT_WINDOW]
        self._fails[key] = stamps
        return stamps

    def locked_for(self, *keys):
        now = time.time()
        with self._lock:
            waits = []
            for key in keys:
                stamps = self._recent(key, now)
                if len(stamps) >= LOGIN_LOCKOUT_FAILURES:
                    waits.append(LOGIN_LOCKOUT_WINDOW - (now - stamps[0]))
            return max(waits) if waits else 0

    def fail(self, *keys):
        now = time.time()
        with self._lock:
            for key in keys:
                self._recent(key, now).append(now)

    def clear(self, *keys):
        with self._lock:
            for key in keys:
                self._fails.pop(key, None)


throttle = LoginThrottle()


# ---------------------------------------------------------------------------
# Session helpers
# ---------------------------------------------------------------------------

def start_session(db, email):
    row = db.execute("SELECT session_version FROM users WHERE email = ?", (email,)).fetchone()
    db.execute("UPDATE users SET last_login_at = ? WHERE email = ?", (_now(), email))
    db.commit()
    session.clear()
    session.permanent = True
    session["email"] = email
    session["sv"] = row["session_version"] if row else 1


def session_email(db):
    """The signed-in account's email, or None. Rejects a session whose
    session_version is stale (password changed / signed out everywhere)."""
    email = session.get("email")
    if not email:
        return None
    row = db.execute("SELECT session_version FROM users WHERE email = ?", (email,)).fetchone()
    if row is None or row["session_version"] != session.get("sv"):
        session.clear()
        return None
    return email


# ---------------------------------------------------------------------------
# SSO (OpenID Connect)
# ---------------------------------------------------------------------------

def load_sso_providers():
    """Reads SSO providers from auth_config.json, then CT_SSO_* env vars
    (env wins). Returns {id: config}. Config keys:

        client_id, client_secret     — from the provider's app registration
        issuer                       — OIDC issuer URL (preset for google/microsoft)
        label                        — button text
        tenant                       — microsoft only: tenant id, or 'organizations'
        allowed_domains              — optional list: only these email domains may sign in
        trust_email                  — accept an email without an email_verified
                                       claim (only for single-tenant corporate IdPs)

    auth_config.json shape: {"sso": {"google": {"client_id": "...", ...}},
    "allow_signup": true}. Env: CT_SSO_PROVIDERS=google,okta then
    CT_SSO_GOOGLE_CLIENT_ID, CT_SSO_GOOGLE_CLIENT_SECRET, CT_SSO_OKTA_ISSUER..."""
    providers = {}
    file_cfg = _file_config()
    for pid, cfg in (file_cfg.get("sso") or {}).items():
        providers[pid.lower()] = dict(cfg)
    for pid in filter(None, (p.strip().lower() for p in os.environ.get("CT_SSO_PROVIDERS", "").split(","))):
        cfg = providers.setdefault(pid, {})
        for key in ("client_id", "client_secret", "issuer", "label", "tenant", "allowed_domains", "trust_email"):
            value = os.environ.get(f"CT_SSO_{pid.upper()}_{key.upper()}")
            if value is not None:
                cfg[key] = value
    result = {}
    for pid, cfg in providers.items():
        merged = {**SSO_PRESETS.get(pid, {}), **{k: v for k, v in cfg.items() if v not in (None, "")}}
        if isinstance(merged.get("allowed_domains"), str):
            merged["allowed_domains"] = [d.strip().lower() for d in merged["allowed_domains"].split(",") if d.strip()]
        if isinstance(merged.get("trust_email"), str):
            merged["trust_email"] = merged["trust_email"].lower() in ("1", "true", "yes")
        if "{tenant}" in merged.get("issuer", ""):
            merged["issuer_template"] = merged["issuer"]
            merged["issuer"] = merged["issuer"].replace("{tenant}", merged.get("tenant") or "organizations")
        if merged.get("client_id") and merged.get("issuer"):
            result[pid] = merged
    return result


def _file_config():
    if not AUTH_CONFIG_PATH.exists():
        return {}
    try:
        return json.loads(AUTH_CONFIG_PATH.read_text(encoding="utf-8"))
    except ValueError as e:
        print(f"[auth] ignoring {AUTH_CONFIG_PATH.name}: not valid JSON ({e})")
        return {}


def signup_allowed():
    env = os.environ.get("CT_ALLOW_SIGNUP")
    if env is not None:
        return env == "1"
    return bool(_file_config().get("allow_signup", True))


_discovery_cache = {}


def _http_json(url, data=None):
    req = urllib.request.Request(url, data=data, headers={"Accept": "application/json"})
    if data is not None:
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise AuthError(f"the identity provider returned {e.code}: {detail}") from e
    except (urllib.error.URLError, OSError, ValueError) as e:
        raise AuthError(f"couldn't reach the identity provider: {e}") from e


def discover(provider):
    issuer = provider["issuer"].rstrip("/")
    cached = _discovery_cache.get(issuer)
    if cached and time.time() - cached[0] < 3600:
        return cached[1]
    doc = _http_json(f"{issuer}/.well-known/openid-configuration")
    _discovery_cache[issuer] = (time.time(), doc)
    return doc


def _b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _jwt_claims(token):
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload))
    except (IndexError, ValueError) as e:
        raise AuthError("the identity provider returned an unreadable ID token") from e


def validate_id_token(claims, provider, nonce, now=None):
    """Checks issuer, audience, expiry and nonce. The token's signature isn't
    checked: it came straight from the provider's token endpoint over TLS
    (authorization-code flow), which OpenID Connect Core §3.1.3.7 allows in
    place of signature validation. Returns the verified email (lowercase)."""
    now = now or time.time()
    issuer = claims.get("iss", "")
    template = provider.get("issuer_template")
    if template and provider.get("tenant") in ("organizations", "common"):
        expected = template.replace("{tenant}", str(claims.get("tid", "")))
    else:
        expected = provider["issuer"]
    if issuer.rstrip("/") != expected.rstrip("/"):
        raise AuthError("the ID token was issued by an unexpected provider")
    aud = claims.get("aud")
    if provider["client_id"] not in (aud if isinstance(aud, list) else [aud]):
        raise AuthError("the ID token isn't meant for this app")
    if float(claims.get("exp", 0)) < now - 60:
        raise AuthError("the sign-in expired — try again")
    if not nonce or not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
        raise AuthError("the sign-in response didn't match this browser session — try again")

    email = (claims.get("email") or "").strip().lower()
    if not email or "@" not in email:
        raise AuthError("the identity provider didn't share an email address — allow the 'email' scope")
    verified = claims.get("email_verified")
    if isinstance(verified, str):
        verified = verified.lower() == "true"
    if verified is False or (verified is None and not provider.get("trust_email")):
        raise AuthError("the identity provider hasn't verified this email address")
    domains = provider.get("allowed_domains")
    if domains and email.split("@", 1)[1] not in domains:
        raise AuthError(f"sign-in is limited to {', '.join(domains)} accounts")
    return email


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def init_app(app, get_db, notify=None):
    """Registers auth routes, the CSRF guard and the session settings."""
    configure_app(app)
    bp = Blueprint("auth", __name__)

    @app.before_request
    def csrf_guard():
        if request.method in ("POST", "PUT", "PATCH", "DELETE") and request.path.startswith("/api/") \
                and not request.path.startswith(CSRF_EXEMPT_PREFIXES):
            if request.headers.get(CSRF_HEADER) != "1":
                return jsonify({"error": "missing request header — reload the page and try again"}), 403
        return None

    def client_ip():
        return request.remote_addr or "?"

    def public_base_url():
        return (os.environ.get("CT_PUBLIC_URL") or request.host_url).rstrip("/")

    @bp.route("/api/auth/config")
    def auth_config():
        providers = load_sso_providers()
        return jsonify({
            "password_enabled": True,
            "signup_enabled": signup_allowed(),
            "min_password_length": MIN_PASSWORD_LENGTH,
            "sso": [{"id": pid, "label": cfg.get("label") or pid.title()} for pid, cfg in providers.items()],
        })

    @bp.route("/api/auth/signup", methods=["POST"])
    def signup():
        if not signup_allowed():
            audit_logger.info("auth event=signup_refused reason=signup_disabled ip=%s", client_ip())
            return jsonify({"error": "new accounts are turned off — ask the administrator, or sign in with SSO"}), 403
        data = request.get_json(force=True) or {}
        email = (data.get("email") or "").strip().lower()
        password = data.get("password") or ""
        name = (data.get("name") or "").strip()[:120] or None
        if not email or "@" not in email or "." not in email.split("@")[-1]:
            return jsonify({"error": "a valid email is required"}), 400
        problem = password_problem(password)
        if problem:
            return jsonify({"error": f"password: {problem}"}), 400
        db = get_db()
        migrate(db)
        existing = db.execute("SELECT password_hash FROM users WHERE email = ?", (email,)).fetchone()
        if existing is not None:
            audit(db, "signup_refused", email, "account exists")
            db.close()
            if existing["password_hash"]:
                return jsonify({"error": "an account with this email already exists — sign in instead"}), 409
            return jsonify({"error": "this account was created before passwords existed. Sign in with SSO, or set "
                                     "a password on the app's computer: python src/manage_users.py set-password "
                                     + email}), 409
        # Two signups for the same email can both pass the check above; the
        # UNIQUE constraint decides, and the loser gets the same 409.
        try:
            db.execute(
                "INSERT INTO users (email, created_at, password_hash, display_name, session_version) VALUES (?, ?, ?, ?, 1)",
                (email, _now(), hash_password(password), name),
            )
            db.commit()
        except sqlite3.IntegrityError:
            db.rollback()
            db.close()
            return jsonify({"error": "an account with this email already exists — sign in instead"}), 409
        except sqlite3.OperationalError as e:
            db.rollback()
            db.close()
            audit_logger.warning("signup for %s failed: %s", email, e)
            return jsonify({"error": "the server is busy — try again in a moment"}), 503
        audit(db, "signup", email)
        start_session(db, email)
        db.close()
        return jsonify({"email": email}), 201

    @bp.route("/api/auth/login", methods=["POST"])
    def login():
        data = request.get_json(force=True) or {}
        email = (data.get("email") or "").strip().lower()
        password = data.get("password") or ""
        keys = (f"email:{email}", f"ip:{client_ip()}")
        wait = throttle.locked_for(*keys)
        if wait:
            audit_logger.info("auth event=login_locked email=%s ip=%s", email, client_ip())
            return jsonify({"error": f"too many failed attempts — try again in {int(wait // 60) + 1} min"}), 429
        db = get_db()
        migrate(db)
        row = db.execute("SELECT password_hash FROM users WHERE email = ?", (email,)).fetchone()
        # Same message whether the email exists or not; check a dummy hash so
        # timing doesn't reveal it either.
        stored = row["password_hash"] if row and row["password_hash"] else _DUMMY_HASH
        valid = check_password_hash(stored, password) and stored is not _DUMMY_HASH
        if not valid:
            throttle.fail(*keys)
            audit(db, "login_failed", email, None if row else "unknown account")
            db.close()
            if row is not None and not row["password_hash"]:
                return jsonify({"error": "this account has no password yet — sign in with SSO, or run "
                                         "python src/manage_users.py set-password " + email}), 401
            return jsonify({"error": "wrong email or password"}), 401
        throttle.clear(*keys)
        if needs_rehash(stored):
            db.execute("UPDATE users SET password_hash = ? WHERE email = ?", (hash_password(password), email))
            db.commit()
        audit(db, "login", email)
        start_session(db, email)
        db.close()
        return jsonify({"email": email})

    @bp.route("/api/me")
    def me():
        db = get_db()
        email = session_email(db)
        if not email:
            db.close()
            return jsonify({"error": "not signed in"}), 401
        row = db.execute("SELECT display_name, password_hash FROM users WHERE email = ?", (email,)).fetchone()
        identities = [r["provider"] for r in db.execute(
            "SELECT provider FROM user_identities WHERE user_email = ? ORDER BY provider", (email,)).fetchall()]
        db.close()
        return jsonify({"email": email, "display_name": row["display_name"], "has_password": bool(row["password_hash"]),
                        "sso_identities": identities})

    @bp.route("/api/logout", methods=["POST"])
    def logout():
        email = session.get("email")
        session.clear()
        if email:
            db = get_db()
            migrate(db)
            audit(db, "logout", email)
            db.close()
        return jsonify({"ok": True})

    @bp.route("/api/auth/logout-everywhere", methods=["POST"])
    def logout_everywhere():
        db = get_db()
        email = session_email(db)
        if not email:
            db.close()
            return jsonify({"error": "not signed in"}), 401
        db.execute("UPDATE users SET session_version = session_version + 1 WHERE email = ?", (email,))
        db.commit()
        audit(db, "logout_everywhere", email)
        db.close()
        session.clear()
        return jsonify({"ok": True})

    @bp.route("/api/auth/password", methods=["POST"])
    def change_password():
        db = get_db()
        email = session_email(db)
        if not email:
            db.close()
            return jsonify({"error": "not signed in"}), 401
        data = request.get_json(force=True) or {}
        row = db.execute("SELECT password_hash FROM users WHERE email = ?", (email,)).fetchone()
        if row["password_hash"] and not check_password_hash(row["password_hash"], data.get("current_password") or ""):
            audit(db, "password_change_failed", email, "wrong current password")
            db.close()
            return jsonify({"error": "your current password is wrong"}), 400
        new = data.get("new_password") or ""
        problem = password_problem(new)
        if problem:
            db.close()
            return jsonify({"error": f"password: {problem}"}), 400
        db.execute("UPDATE users SET password_hash = ?, session_version = session_version + 1 WHERE email = ?",
                   (hash_password(new), email))
        db.commit()
        audit(db, "password_changed", email)
        start_session(db, email)  # keep this browser signed in; every other session ends
        db.close()
        return jsonify({"ok": True})

    @bp.route("/api/identify", methods=["POST"])
    def identify_removed():
        return jsonify({"error": "signing in with just an email was removed in 0.9.0 — use your password or SSO"}), 410

    @bp.route("/auth/sso/<provider_id>/start")
    def sso_start(provider_id):
        provider = load_sso_providers().get(provider_id)
        if provider is None:
            return redirect("/?error=" + urllib.parse.quote("that sign-in option isn't configured"))
        try:
            meta = discover(provider)
        except AuthError as e:
            return redirect("/?error=" + urllib.parse.quote(str(e)))
        state, nonce, verifier = secrets.token_urlsafe(24), secrets.token_urlsafe(24), secrets.token_urlsafe(48)
        session["sso"] = {"provider": provider_id, "state": state, "nonce": nonce, "verifier": verifier,
                          "started": time.time()}
        params = {
            "response_type": "code",
            "client_id": provider["client_id"],
            "redirect_uri": f"{public_base_url()}/auth/sso/{provider_id}/callback",
            "scope": "openid email profile",
            "state": state,
            "nonce": nonce,
            "code_challenge": _b64url(hashlib.sha256(verifier.encode()).digest()),
            "code_challenge_method": "S256",
            "prompt": "select_account",
        }
        return redirect(meta["authorization_endpoint"] + "?" + urllib.parse.urlencode(params))

    @bp.route("/auth/sso/<provider_id>/callback")
    def sso_callback(provider_id):
        pending = session.pop("sso", None)
        try:
            if request.args.get("error"):
                raise AuthError(request.args.get("error_description") or request.args["error"])
            provider = load_sso_providers().get(provider_id)
            if provider is None or not pending or pending.get("provider") != provider_id:
                raise AuthError("this sign-in wasn't started here — try again")
            if time.time() - pending.get("started", 0) > SSO_STATE_TTL:
                raise AuthError("the sign-in took too long — try again")
            if not secrets.compare_digest(request.args.get("state", ""), pending["state"]):
                raise AuthError("the sign-in response didn't match this browser session — try again")
            meta = discover(provider)
            token = _http_json(meta["token_endpoint"], urllib.parse.urlencode({
                "grant_type": "authorization_code",
                "code": request.args.get("code", ""),
                "redirect_uri": f"{public_base_url()}/auth/sso/{provider_id}/callback",
                "client_id": provider["client_id"],
                "client_secret": provider.get("client_secret", ""),
                "code_verifier": pending["verifier"],
            }).encode())
            if not token.get("id_token"):
                raise AuthError("the identity provider didn't return an ID token")
            claims = _jwt_claims(token["id_token"])
            email = validate_id_token(claims, provider, pending["nonce"])
            sign_in_sso_user(get_db, provider_id, str(claims.get("sub")), email, claims.get("name"))
        except AuthError as e:
            audit_logger.info("auth event=sso_failed provider=%s ip=%s reason=%s", provider_id, client_ip(), e)
            return redirect("/?error=" + urllib.parse.quote(str(e)))
        return redirect("/dashboard")

    def sign_in_sso_user(get_db, provider_id, subject, email, name):
        db = get_db()
        migrate(db)
        linked = db.execute("SELECT user_email FROM user_identities WHERE provider = ? AND subject = ?",
                            (provider_id, subject)).fetchone()
        if linked:
            email = linked["user_email"]
        else:
            user = db.execute("SELECT email FROM users WHERE email = ?", (email,)).fetchone()
            if user is None:
                if not signup_allowed():
                    audit(db, "sso_refused", email, f"{provider_id}: signup disabled")
                    db.close()
                    raise AuthError("there's no account for this email and new accounts are turned off")
                db.execute("INSERT INTO users (email, created_at, display_name, session_version) VALUES (?, ?, ?, 1)",
                           (email, _now(), (name or "")[:120] or None))
            db.execute("INSERT INTO user_identities (user_email, provider, subject, created_at) VALUES (?, ?, ?, ?)",
                       (email, provider_id, subject, _now()))
        db.execute("UPDATE user_identities SET last_used_at = ? WHERE provider = ? AND subject = ?",
                   (_now(), provider_id, subject))
        db.commit()
        audit(db, "sso_login", email, provider_id)
        start_session(db, email)
        db.close()

    app.register_blueprint(bp)


_DUMMY_HASH = hash_password(secrets.token_urlsafe(16))
