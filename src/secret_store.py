"""
Encryption at rest for stored credentials (API keys, SMTP password, Slack /
Jira tokens, webhook secrets).

Values are encrypted with Fernet (AES-128-CBC + HMAC-SHA256, from the
`cryptography` package) and stored as "enc:v1:<token>". Anything without
that prefix is treated as legacy plain text and returned unchanged, so a DB
created before 0.8.0 keeps working until migrate_db() re-writes it.

The key comes from, in order:
  1. the COMMITMENT_TRACKER_SECRET_KEY environment variable (a Fernet key —
     generate one with `python -c "from cryptography.fernet import Fernet;
     print(Fernet.generate_key().decode())"`);
  2. secret.key in the project root (next to commitment_tracker.db), created
     automatically on first use.

Losing the key makes every saved credential unreadable — the app keeps
running, but each integration has to be re-entered in Integrations.
"""

import json
import os
from pathlib import Path

from cryptography.fernet import Fernet, InvalidToken

PREFIX = "enc:v1:"
# Runtime data (the DB, this key, Google tokens) lives in the project root,
# one level above src/ where the code is.
KEY_PATH = Path(__file__).resolve().parent.parent / "secret.key"

_fernet = None


class SecretUnreadable(Exception):
    """A stored value was encrypted with a different key (or is corrupted)."""


def _load_key():
    env_key = os.environ.get("COMMITMENT_TRACKER_SECRET_KEY", "").strip()
    if env_key:
        return env_key.encode()
    if KEY_PATH.exists():
        return KEY_PATH.read_bytes().strip()
    key = Fernet.generate_key()
    KEY_PATH.write_bytes(key)
    print(f"[secrets] generated a new encryption key at {KEY_PATH} — back it up; without it saved credentials can't be read")
    return key


def get_fernet():
    global _fernet
    if _fernet is None:
        _fernet = Fernet(_load_key())
    return _fernet


def reset():
    """Forgets the cached key, so the next call re-reads env / secret.key (used by tests and key rotation)."""
    global _fernet
    _fernet = None


def is_encrypted(value):
    return isinstance(value, str) and value.startswith(PREFIX)


def encrypt(value):
    if value is None or value == "" or is_encrypted(value):
        return value
    return PREFIX + get_fernet().encrypt(value.encode("utf-8")).decode("ascii")


def decrypt(value):
    """Returns the plain text. Legacy unencrypted values pass through.
    Raises SecretUnreadable if the value was encrypted with another key."""
    if not is_encrypted(value):
        return value
    try:
        return get_fernet().decrypt(value[len(PREFIX):].encode("ascii")).decode("utf-8")
    except InvalidToken:
        raise SecretUnreadable(
            "a saved credential can't be decrypted — the encryption key (secret.key or "
            "COMMITMENT_TRACKER_SECRET_KEY) has changed since it was saved; re-enter it in Integrations"
        ) from None


def dump_config(config):
    """dict -> encrypted string for a config_json column."""
    return encrypt(json.dumps(config or {}))


def load_config(stored, default_on_error=False):
    """config_json column -> dict. With default_on_error, an unreadable value
    comes back as {} instead of raising (for read-only screens)."""
    try:
        return json.loads(decrypt(stored) or "{}")
    except SecretUnreadable:
        if default_on_error:
            return {}
        raise
