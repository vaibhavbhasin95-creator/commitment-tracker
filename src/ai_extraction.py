"""
AI extraction engine (v0.5.0) — optional, self-contained feature.

Lets an account choose which engine extracts commitments from transcripts:

  * "builtin"           — the local ML classifier in transcript_parser.py (default;
                          free, nothing leaves the machine). Identical to v0.4.0.
  * "anthropic"         — Claude via the Anthropic API, with the customer's own API key.
  * "openai_compatible" — any /chat/completions endpoint with the customer's own key:
                          OpenAI, Azure OpenAI, Google Gemini (OpenAI-compatible
                          endpoint), Groq, a local Ollama server, ...

Plus a comparison tool: run the built-in model and the chosen AI engine on the same
saved transcript side by side (nothing is written to the meeting), mark which items
are correct, and see each engine's precision across all reviews.

Isolation — everything for this feature lives here and in static/ai-extraction.html:
  * its own tables (ai_extraction_settings, ai_extraction_runs, ai_extraction_comparisons),
    created on first use; no existing table is altered;
  * its own routes under /api/ai-extraction/* and the /ai-extraction page;
  * app.py only calls try_extract()/try_extract_from_turns() at the start of its two
    extraction helpers, and registers this module via init_app(). When an account uses
    "builtin" (the default) or anything goes wrong here, those calls return None and
    app.py's v0.4.0 path runs unchanged.
Turn the whole feature off with the environment variable AI_EXTRACTION_ENABLED=0.
To remove it entirely, see CHANGELOG.md 0.5.0 -> "How to revert".
"""

import json
import re
from datetime import date, datetime

from flask import Blueprint, jsonify, request, send_from_directory

import secret_store

ENGINES = ("builtin", "anthropic", "openai_compatible")
ENGINE_LABELS = {"builtin": "Built-in model", "anthropic": "Claude (Anthropic API)", "openai_compatible": "OpenAI-compatible API"}
DEFAULT_ANTHROPIC_MODEL = "claude-opus-5"
# Opus 5 / Fable 5.1 safety classifiers can decline a request; opt into the
# server-side fallback so a false-positive decline doesn't lose the extraction.
SERVER_FALLBACK_MODELS = {"claude-opus-5", "claude-fable-5-1"}
REQUEST_TIMEOUT_SECONDS = 300

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS ai_extraction_settings (
    user_email TEXT PRIMARY KEY,
    engine TEXT NOT NULL DEFAULT 'builtin',
    config_json TEXT NOT NULL DEFAULT '{}',
    consent_at TEXT,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_extraction_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_email TEXT NOT NULL,
    meeting_id INTEGER,
    engine TEXT NOT NULL,
    model TEXT,
    status TEXT NOT NULL,              -- 'ok' | 'fell_back' (AI failed, built-in used)
    commitments INTEGER,
    detail TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS ai_extraction_comparisons (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_email TEXT NOT NULL,
    meeting_id INTEGER NOT NULL,
    engine TEXT NOT NULL,
    model TEXT,
    builtin_json TEXT NOT NULL,        -- list of extracted items, or {"error": ...}
    ai_json TEXT NOT NULL,
    verdicts_json TEXT NOT NULL DEFAULT '{}',   -- {"builtin": [true|false|null...], "ai": [...]}
    created_at TEXT NOT NULL
);
"""

SYSTEM_PROMPT = """You extract commitments from meeting transcripts for a task tracker.

A commitment is something a specific person agreed, promised, or was assigned to do after the meeting. It is not a statement of fact, an opinion, a proposal nobody accepted, a question, or a summary of the discussion.

For each commitment return:
- task_text: the task as a short imperative phrase, e.g. "Send the pricing deck to sales".
- owner_name: the person responsible, as named in the transcript. When one person reads out assignments for others ("Kevin (Due: Sept 27): model the cash flow"), the owner is the person assigned, not the reader. Use "UNRESOLVED" if nobody clearly owns it.
- due_date: YYYY-MM-DD, resolving relative dates ("Friday", "end of this month") against the meeting date given below. Empty string if no deadline was stated.
- confidence: 0 to 1, how sure you are this is a real commitment with the right owner.
- source_quote: the sentence from the transcript that states it, copied verbatim.

Return every distinct commitment once. If there are none, return an empty list."""

OUTPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "commitments": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "task_text": {"type": "string"},
                    "owner_name": {"type": "string"},
                    "due_date": {"type": "string"},
                    "confidence": {"type": "number"},
                    "source_quote": {"type": "string"},
                },
                "required": ["task_text", "owner_name", "due_date", "confidence", "source_quote"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["commitments"],
    "additionalProperties": False,
}

SAMPLE_TRANSCRIPT = (
    "Priya: I'll send the updated pricing deck to sales by Thursday.\n"
    "Karthik: Thanks. Arjun, can you schedule the vendor retest this week?\n"
    "Arjun: Sure, I'll book it for Wednesday.\n"
    "Priya: The dashboard looks good overall."
)


class AIExtractionError(RuntimeError):
    """Any failure calling an AI engine — shown to the user, never raised into app.py."""


def _now():
    return datetime.utcnow().isoformat(timespec="seconds")


def ensure_schema(db):
    db.executescript(SCHEMA_SQL)


# ---------------------------------------------------------------------------
# Settings
# ---------------------------------------------------------------------------

def get_settings(db, email):
    """config_json holds one config per engine ({"anthropic": {...},
    "openai_compatible": {...}}) so switching engines — including back to
    built-in — never discards a saved key. "config" is the active engine's."""
    ensure_schema(db)
    row = db.execute("SELECT * FROM ai_extraction_settings WHERE user_email = ?", (email,)).fetchone()
    if row is None:
        return {"engine": "builtin", "config": {}, "configs": {}, "consent_at": None}
    # Encrypted at rest (0.8.0). If it was saved under a different key the
    # engine has no key and falls back to built-in until it's re-entered.
    configs = secret_store.load_config(row["config_json"], default_on_error=True)
    return {"engine": row["engine"], "config": configs.get(row["engine"], {}), "configs": configs, "consent_at": row["consent_at"]}


def _masked(config):
    shown = dict(config)
    key = shown.pop("api_key", "")
    shown["api_key_hint"] = f"••••{key[-4:]}" if key else ""
    shown["has_api_key"] = bool(key)
    return shown


def _model_for(engine, config):
    if engine == "anthropic":
        return (config.get("model") or "").strip() or DEFAULT_ANTHROPIC_MODEL
    if engine == "openai_compatible":
        return (config.get("model") or "").strip()
    return None


# ---------------------------------------------------------------------------
# Engines
# ---------------------------------------------------------------------------

def _user_message(transcript_text, meeting_date):
    return f"Meeting date: {meeting_date}\n\nTranscript:\n{transcript_text}"


def _call_anthropic(config, transcript_text, meeting_date):
    try:
        import anthropic
    except ImportError as e:
        raise AIExtractionError("the anthropic package isn't installed — run: pip install anthropic") from e

    api_key = (config.get("api_key") or "").strip()
    if not api_key:
        raise AIExtractionError("no Anthropic API key saved")
    model = _model_for("anthropic", config)
    client = anthropic.Anthropic(api_key=api_key, timeout=REQUEST_TIMEOUT_SECONDS)
    params = dict(
        model=model,
        max_tokens=16000,
        system=SYSTEM_PROMPT,
        messages=[{"role": "user", "content": _user_message(transcript_text, meeting_date)}],
        output_config={"format": {"type": "json_schema", "schema": OUTPUT_SCHEMA}},
    )
    try:
        if model in SERVER_FALLBACK_MODELS:
            response = client.beta.messages.create(
                betas=["server-side-fallback-2026-07-01"], fallbacks="default", **params
            )
        else:
            response = client.messages.create(**params)
    except anthropic.AuthenticationError as e:
        raise AIExtractionError("Anthropic rejected the API key") from e
    except anthropic.PermissionDeniedError as e:
        raise AIExtractionError("this API key isn't allowed to use that model") from e
    except anthropic.NotFoundError as e:
        raise AIExtractionError(f"model '{model}' wasn't found") from e
    except anthropic.RateLimitError as e:
        raise AIExtractionError("rate limited by Anthropic — try again shortly") from e
    except anthropic.APIStatusError as e:
        raise AIExtractionError(f"Anthropic API error {e.status_code}: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise AIExtractionError("couldn't reach the Anthropic API (network or timeout)") from e

    if response.stop_reason == "refusal":
        raise AIExtractionError("the model declined this transcript")
    if response.stop_reason == "max_tokens":
        raise AIExtractionError("the response was cut off (transcript too long for one request)")
    text = next((b.text for b in response.content if b.type == "text"), "")
    return _parse_items(text), response.model


def _call_openai_compatible(config, transcript_text, meeting_date):
    import urllib.error
    import urllib.request

    base_url = (config.get("base_url") or "").strip().rstrip("/")
    api_key = (config.get("api_key") or "").strip()
    model = _model_for("openai_compatible", config)
    if not base_url or not model:
        raise AIExtractionError("base URL and model are required")

    body = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT + "\n\nReply with only a JSON object of the form "
             '{"commitments": [{"task_text": "", "owner_name": "", "due_date": "", "confidence": 0.0, "source_quote": ""}]}.'},
            {"role": "user", "content": _user_message(transcript_text, meeting_date)},
        ],
        "response_format": {"type": "json_object"},
    }
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
        headers["api-key"] = api_key  # Azure OpenAI reads this header instead
    req = urllib.request.Request(
        f"{base_url}/chat/completions", data=json.dumps(body).encode("utf-8"), headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT_SECONDS) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")[:300]
        raise AIExtractionError(f"API error {e.code}: {detail}") from e
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise AIExtractionError(f"couldn't reach {base_url}: {e}") from e
    try:
        choice = payload["choices"][0]
        text = choice["message"]["content"] or ""
    except (KeyError, IndexError, TypeError) as e:
        raise AIExtractionError("unexpected response shape from the API") from e
    if choice.get("finish_reason") == "length":
        raise AIExtractionError("the response was cut off (transcript too long for one request)")
    return _parse_items(text), payload.get("model") or model


def _parse_items(text):
    """Validates the engine's JSON into the same dict shape transcript_parser
    returns, so app.py can insert it with its existing code."""
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        data = json.loads(cleaned)
    except ValueError as e:
        raise AIExtractionError("the model's reply wasn't valid JSON") from e
    items = data.get("commitments") if isinstance(data, dict) else None
    if not isinstance(items, list):
        raise AIExtractionError("the model's reply had no commitments list")

    results = []
    for item in items:
        if not isinstance(item, dict):
            continue
        task = str(item.get("task_text") or "").strip()
        quote = str(item.get("source_quote") or "").strip() or task
        if not task:
            continue
        due = str(item.get("due_date") or "").strip()
        try:
            due = date.fromisoformat(due).isoformat() if due else None
        except ValueError:
            due = None
        try:
            confidence = min(max(float(item.get("confidence")), 0.0), 1.0)
        except (TypeError, ValueError):
            confidence = None
        results.append({
            "task_text": task[:300],
            "owner_name": str(item.get("owner_name") or "").strip() or "UNRESOLVED",
            "due_date": due,
            "confidence_score": confidence,
            "source_quote": quote,
        })
    return results


def run_engine(engine, config, transcript_text, meeting_date):
    """Returns (items, model_used). Raises AIExtractionError."""
    if engine == "anthropic":
        return _call_anthropic(config, transcript_text, meeting_date)
    if engine == "openai_compatible":
        return _call_openai_compatible(config, transcript_text, meeting_date)
    raise AIExtractionError(f"unknown engine '{engine}'")


# ---------------------------------------------------------------------------
# Hook used by app.py's extraction helpers
# ---------------------------------------------------------------------------

def try_extract(db, meeting_id, meeting_date, transcript_text):
    """Called at the top of app.auto_extract_commitments. Returns a list of
    extracted commitments when the meeting owner's account uses an AI engine
    and the call succeeds; returns None in every other case (built-in engine,
    no settings, missing consent, AI failure) so app.py continues with its
    unchanged built-in path. Never raises.

    Side effect when an AI engine is used: commits the caller's open
    transaction (the meeting row it just created) before the network call, so
    the database isn't write-locked for the duration of a slow AI request.
    The meeting was going to be kept regardless of extraction outcome."""
    try:
        meeting = db.execute("SELECT owner_email FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
        if meeting is None or not transcript_text:
            return None
        email = meeting["owner_email"]
        settings = get_settings(db, email)
        engine = settings["engine"]
        if engine == "builtin" or not settings["consent_at"]:
            return None
        db.commit()
    except Exception:
        return None

    model = _model_for(engine, settings["config"])
    try:
        items, model = run_engine(engine, settings["config"], transcript_text, meeting_date)
    except Exception as e:  # any failure -> built-in path takes over
        _log_run(db, email, meeting_id, engine, model, "fell_back", None,
                 f"{e} — used the built-in model instead")
        return None
    _log_run(db, email, meeting_id, engine, model, "ok", len(items), None)
    return items


def try_extract_from_turns(db, meeting_id, meeting_date, turns):
    """Same as try_extract, for sources that deliver speaker turns (Fathom)."""
    text = "\n".join(f"{t['speaker']}: {t['text']}" for t in turns or [])
    return try_extract(db, meeting_id, meeting_date, text)


def engine_label(db, meeting_id):
    """'Claude (Anthropic API) · <model>' for the AI engine the meeting
    owner's account has chosen and confirmed, or None when it uses the
    built-in model. app.py shows it so a user can see which engine read a
    transcript."""
    try:
        meeting = db.execute("SELECT owner_email FROM meetings WHERE id = ?", (meeting_id,)).fetchone()
        if meeting is None:
            return None
        s = get_settings(db, meeting["owner_email"])
    except Exception:
        return None
    if s["engine"] == "builtin" or not s["consent_at"]:
        return None
    model = _model_for(s["engine"], s["config"])
    return ENGINE_LABELS[s["engine"]] + (f" · {model}" if model else "")


def _log_run(db, email, meeting_id, engine, model, status, count, detail):
    try:
        db.execute(
            "INSERT INTO ai_extraction_runs (user_email, meeting_id, engine, model, status, commitments, detail, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (email, meeting_id, engine, model, status, count, detail, _now()),
        )
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

def init_app(app, get_db, require_email, transcript_parser):
    """Registers this feature's page and API routes on the Flask app."""
    bp = Blueprint("ai_extraction", __name__)

    def unauthorized():
        return jsonify({"error": "not identified"}), 401

    @bp.route("/ai-extraction")
    def page():
        return send_from_directory(app.static_folder, "ai-extraction.html")

    @bp.route("/api/ai-extraction/status")
    def status():
        return jsonify({"enabled": True})

    @bp.route("/api/ai-extraction/settings", methods=["GET"])
    def read_settings():
        email = require_email()
        if not email:
            return unauthorized()
        db = get_db()
        s = get_settings(db, email)
        db.close()
        return jsonify({
            "engine": s["engine"],
            "configs": {name: _masked(cfg) for name, cfg in s["configs"].items()},
            "consent_at": s["consent_at"],
        })

    @bp.route("/api/ai-extraction/settings", methods=["POST"])
    def save_settings():
        email = require_email()
        if not email:
            return unauthorized()
        data = request.get_json(force=True)
        engine = data.get("engine", "builtin")
        if engine not in ENGINES:
            return jsonify({"error": f"engine must be one of {', '.join(ENGINES)}"}), 400

        db = get_db()
        configs = get_settings(db, email)["configs"]
        if engine != "builtin":
            incoming = data.get("config") or {}
            previous = configs.get(engine, {})
            # A blank key field means "keep the saved key" (the UI never receives the key back).
            api_key = (incoming.get("api_key") or "").strip() or previous.get("api_key", "")
            config = {"api_key": api_key, "model": (incoming.get("model") or "").strip()}
            if engine == "openai_compatible":
                config["base_url"] = (incoming.get("base_url") or "").strip()
                if not config["base_url"] or not config["model"]:
                    db.close()
                    return jsonify({"error": "base URL and model are required"}), 400
            elif not api_key:
                db.close()
                return jsonify({"error": "an Anthropic API key is required"}), 400
            if not data.get("consent"):
                db.close()
                return jsonify({"error": "confirm that transcripts may be sent to this provider"}), 400
            configs[engine] = config

        consent_at = _now() if engine != "builtin" else None
        db.execute(
            "INSERT INTO ai_extraction_settings (user_email, engine, config_json, consent_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?) ON CONFLICT(user_email) DO UPDATE SET "
            "engine = excluded.engine, config_json = excluded.config_json, "
            "consent_at = excluded.consent_at, updated_at = excluded.updated_at",
            (email, engine, secret_store.dump_config(configs), consent_at, _now()),
        )
        db.commit()
        db.close()
        return jsonify({"ok": True}), 201

    @bp.route("/api/ai-extraction/test", methods=["POST"])
    def test_connection():
        """Runs the saved engine on a four-line sample transcript."""
        email = require_email()
        if not email:
            return unauthorized()
        db = get_db()
        s = get_settings(db, email)
        db.close()
        if s["engine"] == "builtin":
            return jsonify({"error": "choose and save an AI engine first"}), 400
        try:
            items, model = run_engine(s["engine"], s["config"], SAMPLE_TRANSCRIPT, date.today().isoformat())
        except AIExtractionError as e:
            return jsonify({"error": str(e)}), 502
        return jsonify({"ok": True, "model": model, "sample": SAMPLE_TRANSCRIPT, "commitments": items})

    @bp.route("/api/ai-extraction/runs", methods=["GET"])
    def recent_runs():
        email = require_email()
        if not email:
            return unauthorized()
        db = get_db()
        ensure_schema(db)
        rows = db.execute(
            "SELECT r.*, m.title AS meeting_title FROM ai_extraction_runs r "
            "LEFT JOIN meetings m ON m.id = r.meeting_id WHERE r.user_email = ? ORDER BY r.id DESC LIMIT 15",
            (email,),
        ).fetchall()
        db.close()
        return jsonify([dict(r) for r in rows])

    @bp.route("/api/ai-extraction/compare", methods=["POST"])
    def compare():
        """Runs both engines on a meeting's saved transcript. Read-only for the
        meeting: results are stored only as a comparison record."""
        email = require_email()
        if not email:
            return unauthorized()
        data = request.get_json(force=True)
        db = get_db()
        meeting = db.execute(
            "SELECT id, meeting_date, transcript_excerpt FROM meetings WHERE id = ? AND owner_email = ?",
            (data.get("meeting_id"), email),
        ).fetchone()
        s = get_settings(db, email)
        db.close()
        if meeting is None:
            return jsonify({"error": "meeting not found"}), 404
        if not meeting["transcript_excerpt"]:
            return jsonify({"error": "this meeting has no saved transcript"}), 400
        if s["engine"] == "builtin":
            return jsonify({"error": "choose and save an AI engine first — the comparison runs it against the built-in model"}), 400

        text, meeting_date = meeting["transcript_excerpt"], meeting["meeting_date"]
        if transcript_parser is None:
            builtin = {"error": "the built-in model's libraries aren't installed"}
        else:
            try:
                builtin = transcript_parser.extract_commitments(text, meeting_date=meeting_date)
            except Exception as e:
                builtin = {"error": str(e)}
        model = _model_for(s["engine"], s["config"])
        try:
            ai, model = run_engine(s["engine"], s["config"], text, meeting_date)
        except AIExtractionError as e:
            ai = {"error": str(e)}

        db = get_db()
        cur = db.execute(
            "INSERT INTO ai_extraction_comparisons (user_email, meeting_id, engine, model, builtin_json, ai_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (email, meeting["id"], s["engine"], model, json.dumps(builtin), json.dumps(ai), _now()),
        )
        db.commit()
        comparison_id = cur.lastrowid
        db.close()
        return jsonify({"id": comparison_id, "engine": s["engine"], "model": model, "builtin": builtin, "ai": ai}), 201

    @bp.route("/api/ai-extraction/comparisons/<int:comparison_id>/verdicts", methods=["POST"])
    def save_verdicts(comparison_id):
        email = require_email()
        if not email:
            return unauthorized()
        data = request.get_json(force=True)
        verdicts = {k: [v if v in (True, False) else None for v in (data.get(k) or [])] for k in ("builtin", "ai")}
        db = get_db()
        ensure_schema(db)
        cur = db.execute(
            "UPDATE ai_extraction_comparisons SET verdicts_json = ? WHERE id = ? AND user_email = ?",
            (json.dumps(verdicts), comparison_id, email),
        )
        db.commit()
        db.close()
        if cur.rowcount == 0:
            return jsonify({"error": "comparison not found"}), 404
        return jsonify({"ok": True})

    @bp.route("/api/ai-extraction/scoreboard", methods=["GET"])
    def scoreboard():
        """Precision per engine across every reviewed comparison: of the items
        someone marked, how many were real commitments."""
        email = require_email()
        if not email:
            return unauthorized()
        db = get_db()
        ensure_schema(db)
        rows = db.execute(
            "SELECT c.id, c.engine, c.model, c.created_at, c.builtin_json, c.ai_json, c.verdicts_json, m.title AS meeting_title "
            "FROM ai_extraction_comparisons c LEFT JOIN meetings m ON m.id = c.meeting_id "
            "WHERE c.user_email = ? ORDER BY c.id DESC",
            (email,),
        ).fetchall()
        db.close()

        totals = {}
        history = []
        for r in rows:
            verdicts = json.loads(r["verdicts_json"])
            ai_label = f"{ENGINE_LABELS.get(r['engine'], r['engine'])}" + (f" · {r['model']}" if r["model"] else "")
            entry = {"id": r["id"], "meeting_title": r["meeting_title"], "created_at": r["created_at"], "ai_label": ai_label}
            for side, label in (("builtin", ENGINE_LABELS["builtin"]), ("ai", ai_label)):
                marks = [v for v in verdicts.get(side, []) if v is not None]
                items = json.loads(r[f"{side}_json"])
                entry[f"{side}_found"] = len(items) if isinstance(items, list) else None
                entry[f"{side}_correct"] = sum(1 for v in marks if v)
                entry[f"{side}_reviewed"] = len(marks)
                t = totals.setdefault(label, {"engine": label, "reviewed": 0, "correct": 0})
                t["reviewed"] += len(marks)
                t["correct"] += sum(1 for v in marks if v)
            history.append(entry)
        for t in totals.values():
            t["precision"] = round(t["correct"] / t["reviewed"], 3) if t["reviewed"] else None
        return jsonify({"engines": list(totals.values()), "comparisons": history})

    app.register_blueprint(bp)
