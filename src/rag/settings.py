from datetime import datetime
from urllib.parse import urlparse

import secret_store

from .errors import RagConsentRequired, RagError

LOCAL_HOSTS = ("localhost", "127.0.0.1", "::1")
ENGINE_PREFERENCE = ("openai_compatible", "anthropic")  # tried after the active engine
NO_ENGINE_MESSAGE = "No AI engine is saved. Add one on the AI extraction page (one-time setup)."

RAG_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS rag_settings (
    user_email TEXT PRIMARY KEY,
    consent_at TEXT
);
"""


def _now():
    return datetime.utcnow().isoformat(timespec="seconds")


def _is_local(config: dict) -> bool:
    return urlparse(config.get("base_url") or "").hostname in LOCAL_HOSTS


def _is_usable(engine: str, config: dict) -> bool:
    if not (config.get("model") or "").strip():
        return False
    if engine == "anthropic":
        return bool((config.get("api_key") or "").strip())
    return engine == "openai_compatible" and bool((config.get("base_url") or "").strip())


def _read_consent(db, account_email: str):
    try:
        row = db.execute("SELECT consent_at FROM rag_settings WHERE user_email = ?", (account_email,)).fetchone()
    except Exception:
        return None  # table not created yet: no consent given
    return row["consent_at"] if row else None


def grant_rag_consent(db, account_email: str) -> None:
    """Records that this account's meetings may be sent to its saved cloud engine for RAG."""
    db.executescript(RAG_SCHEMA_SQL)
    db.execute(
        "INSERT INTO rag_settings (user_email, consent_at) VALUES (?, ?) "
        "ON CONFLICT(user_email) DO UPDATE SET consent_at = excluded.consent_at",
        (account_email, _now()),
    )
    db.commit()


def _read_saved(db, account_email: str):
    """(active_engine, configs, extraction_consent_at) from the AI extraction page's settings.
    Read straight from the table, not through ai_extraction.py, so RAG keeps working if that
    optional feature is turned off or reverted."""
    try:
        row = db.execute(
            "SELECT engine, config_json, consent_at FROM ai_extraction_settings WHERE user_email = ?",
            (account_email,),
        ).fetchone()
    except Exception:
        return None, {}, None  # table doesn't exist yet: nobody has saved an engine
    if row is None:
        return None, {}, None
    # Credentials are encrypted at rest. A key saved under a different secret.key comes back empty.
    configs = secret_store.load_config(row["config_json"], default_on_error=True)
    return row["engine"], configs, row["consent_at"]


def _consent_for(db, account_email, engine, config, active, extraction_consent):
    """The consent timestamp that lets this engine be used, or None."""
    if _is_local(config):
        return "local"  # nothing leaves this machine
    if engine == active and extraction_consent:
        return extraction_consent
    return _read_consent(db, account_email)


def load_engine(db, account_email: str) -> dict:
    """
    The AI engine RAG should use, in the shape llm.chat() expects:
    {"engine": ..., "config": {...}, "consent_at": ...}.

    The active extraction engine is tried first, then any other saved engine, so RAG
    works whichever engine extraction uses, including Built-in. Raises RagConsentRequired
    when only a cloud engine is available and RAG has no consent yet, and RagError otherwise.
    """
    active, configs, extraction_consent = _read_saved(db, account_email)
    order = [name for name in (active, *ENGINE_PREFERENCE) if name in ENGINE_PREFERENCE]
    needs_consent = None
    for engine in dict.fromkeys(order):  # de-duplicated, order kept
        config = configs.get(engine) or {}
        if not _is_usable(engine, config):
            continue
        consent = _consent_for(db, account_email, engine, config, active, extraction_consent)
        if consent:
            return {"engine": engine, "config": config, "consent_at": consent}
        needs_consent = needs_consent or engine
    if needs_consent:
        raise RagConsentRequired(
            "Confirm once that your meetings may be sent to your saved AI provider.", provider=needs_consent
        )
    raise RagError(NO_ENGINE_MESSAGE)
