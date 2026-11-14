"""
AI-tailored email nudges (0.9.0).

The account writes ONE draft reminder email (subject + body) on the
Integrations page. For each reminder, the draft's placeholders are filled in
({owner_first_name}, {task}, {due_date}, ...) and — when AI tailoring is on —
an LLM rewrites it for the recipient: the tone, formality, length and level
of detail follow their designation from the People directory (a VP gets a
brief, outcome-focused note; an analyst or intern gets friendlier, more
explicit next steps). Facts — task, due date, meeting, the sender's asks —
are kept exactly; nothing is invented.

Where the AI comes from: the engine already saved on the AI extraction page
(Claude or any OpenAI-compatible API, with the account's own key and its
consent to send data to that provider). It's read straight from that page's
settings table, so this module doesn't import ai_extraction.py and keeps
working — falling back to the plain draft — if that optional feature is
turned off or reverted.

Any AI problem (no engine, no consent, network, bad reply) never blocks a
reminder: the filled-in draft is sent as written, and the reason is noted in
the nudge log.
"""

import json
import re
import urllib.error
import urllib.request

import prompt_safety
import secret_store

TIMEOUT_SECONDS = 60
DEFAULT_ANTHROPIC_MODEL = "claude-opus-5"

DEFAULT_SUBJECT = "{reason}: {task}"
DEFAULT_BODY = (
    "Hi {owner_first_name},\n\n"
    "A quick reminder about the action you took on in \"{meeting}\":\n\n"
    "  {task}\n  Due: {due_date}\n\n"
    "Could you let me know where this stands, or if anything is blocking it?\n\n"
    "Thanks,\n{sender_name}"
)

PLACEHOLDERS = {
    "owner_name": "the task owner's full name",
    "owner_first_name": "their first name",
    "designation": "their designation from People",
    "task": "the task",
    "due_date": "the due date",
    "status": "pending / in progress",
    "meeting": "the meeting it came from",
    "reason": "Due tomorrow / Due today / Overdue",
    "sender_name": "your name",
}

REASON_TEXT = {"test": "Test reminder", "day_before": "Due tomorrow", "on_due": "Due today", "overdue": "Overdue"}

SYSTEM_PROMPT = """You adapt a reminder email that a manager drafted, for one specific recipient.

Adapt tone, formality, length and depth to the recipient's designation (job title / seniority):
- Executives (C-level, VP, Director, Head of): very brief, respectful, outcome-focused; 2-4 sentences; no hand-holding.
- Managers and leads: concise and collaborative; offer help removing blockers.
- Individual contributors (engineer, analyst, specialist, consultant, associate): friendly and clear; state the next step and the deadline plainly.
- Interns, trainees, juniors: warm and supportive; spell out what "done" looks like and invite questions.
- Designation unknown: neutral, polite, professional.
The reminder reason also matters: "Overdue" may be firmer (still courteous); "Due tomorrow" is a light heads-up.

Hard rules:
- Keep every fact exactly as given: task, due date, meeting name, names, and every specific ask or instruction in the draft. Never invent facts, deadlines, consequences, links or promises.
- Keep the sender's intent and sign-off name. Write in the draft's language.
- Plain text only (no markdown, no HTML). Subject line under 90 characters.
- """ + prompt_safety.untrusted_note("reminder") + """
- Tokens such as [EMAIL_1] or [PHONE_1] stand for masked values: keep them exactly as written.
Reply with only a JSON object: {"subject": "...", "body": "...", "tone": "<3-8 words describing the tone you chose>"}"""

# Links in the model's reply must already be in the draft (output check, AI-01).
URL_RE = re.compile(r"(?i)\b(?:https?://|www\.)[^\s<>\"')]+")


class AIWriterError(RuntimeError):
    """Why the AI couldn't tailor the email — shown in the nudge log."""


def fill(template, values):
    """Replaces known {placeholders}; leaves any other braces untouched."""
    return re.sub(r"\{(\w+)\}", lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0), template or "")


def context_for(commitment, reason, designation, sender_name):
    owner = (commitment.get("owner_name") or "").strip()
    first = owner.split(" ")[0] if owner and owner.upper() not in ("UNRESOLVED", "UNKNOWN") else "there"
    return {
        "owner_name": owner or "there",
        "owner_first_name": first,
        "designation": designation or "",
        "task": commitment.get("task_text") or "",
        "due_date": commitment.get("due_date") or "no due date",
        "status": (commitment.get("status") or "pending").replace("_", " "),
        "meeting": commitment.get("meeting_title") or "a recent meeting",
        "reason": REASON_TEXT.get(reason, "Reminder"),
        "sender_name": sender_name or "",
    }


def draft_email(config, values):
    subject = fill(config.get("draft_subject") or DEFAULT_SUBJECT, values).strip()
    body = fill(config.get("draft_body") or DEFAULT_BODY, values).strip()
    return subject[:200], body


# ---------------------------------------------------------------------------
# Engine (borrowed from the AI extraction settings, read-only)
# ---------------------------------------------------------------------------

def load_engine(db, account_email):
    """(engine, config) from the AI extraction page's saved settings.
    Raises AIWriterError if there isn't a usable one."""
    try:
        row = db.execute(
            "SELECT engine, config_json, consent_at FROM ai_extraction_settings WHERE user_email = ?", (account_email,)
        ).fetchone()
    except Exception:
        row = None
    if row is None or row["engine"] in (None, "builtin"):
        raise AIWriterError("no AI engine is set up — choose one on the AI extraction page")
    if not row["consent_at"]:
        raise AIWriterError("the AI engine hasn't been confirmed for sending data on the AI extraction page")
    configs = secret_store.load_config(row["config_json"], default_on_error=True)
    config = configs.get(row["engine"]) or {}
    if not config:
        raise AIWriterError("the AI engine's saved settings can't be read — re-enter them on the AI extraction page")
    return row["engine"], config


def _call_anthropic(config, user_message):
    try:
        import anthropic
    except ImportError as e:
        raise AIWriterError("the anthropic package isn't installed") from e
    api_key = (config.get("api_key") or "").strip()
    if not api_key:
        raise AIWriterError("no Anthropic API key saved")
    model = (config.get("model") or "").strip() or DEFAULT_ANTHROPIC_MODEL
    client = anthropic.Anthropic(api_key=api_key, timeout=TIMEOUT_SECONDS)
    try:
        response = client.messages.create(
            model=model, max_tokens=2000, system=SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_message}],
        )
    except anthropic.APIStatusError as e:
        raise AIWriterError(f"Anthropic API error {e.status_code}: {e.message}") from e
    except anthropic.APIConnectionError as e:
        raise AIWriterError("couldn't reach the Anthropic API") from e
    if response.stop_reason == "refusal":
        raise AIWriterError("the model declined to rewrite this email")
    return "".join(b.text for b in response.content if b.type == "text"), response.model


def _call_openai_compatible(config, user_message):
    base_url = (config.get("base_url") or "").strip().rstrip("/")
    model = (config.get("model") or "").strip()
    if not base_url or not model:
        raise AIWriterError("the OpenAI-compatible engine needs a base URL and model")
    headers = {"Content-Type": "application/json"}
    if (config.get("api_key") or "").strip():
        headers["Authorization"] = f"Bearer {config['api_key'].strip()}"
        headers["api-key"] = config["api_key"].strip()
    body = {"model": model, "response_format": {"type": "json_object"},
            "messages": [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user_message}]}
    req = urllib.request.Request(f"{base_url}/chat/completions", data=json.dumps(body).encode("utf-8"),
                                 headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        return payload["choices"][0]["message"]["content"] or "", payload.get("model") or model
    except urllib.error.HTTPError as e:
        raise AIWriterError(f"API error {e.code}: {e.read().decode('utf-8', 'replace')[:200]}") from e
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise AIWriterError(f"couldn't reach {base_url}: {e}") from e
    except (KeyError, IndexError, TypeError, ValueError) as e:
        raise AIWriterError("unexpected response shape from the API") from e


def _parse(text):
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", (text or "").strip())
    try:
        data = json.loads(cleaned)
    except ValueError as e:
        raise AIWriterError("the model's reply wasn't valid JSON") from e
    subject = str(data.get("subject") or "").strip().replace("\n", " ")
    body = str(data.get("body") or "").strip()
    if not subject or not body:
        raise AIWriterError("the model's reply was missing the subject or body")
    return subject[:200], body, str(data.get("tone") or "").strip()[:80]


def personalise(db, account_email, config, commitment, reason, designation, sender_name, engine_override=None):
    """Returns a dict: subject, body, ai_used (bool), model, tone, note.
    Never raises — on any AI problem returns the filled-in draft with note
    explaining why."""
    values = context_for(commitment, reason, designation, sender_name)
    subject, body = draft_email(config, values)
    result = {"subject": subject, "body": body, "ai_used": False, "model": None, "tone": None, "note": None}
    if (config.get("ai_mode") or "off") != "extraction_engine":
        return result
    try:
        engine, engine_config = engine_override or load_engine(db, account_email)
        message = prompt_safety.fence("reminder", (
            f"Recipient: {values['owner_name']}\n"
            f"Designation: {designation or 'unknown'}\n"
            f"Reminder reason: {values['reason']}\n"
            f"Task: {values['task']}\nDue date: {values['due_date']}\nStatus: {values['status']}\n"
            f"Meeting: {values['meeting']}\nSender: {sender_name or 'the sender'}\n\n"
            f"Draft subject: {subject}\n\nDraft body:\n{body}"
        ))
        # Emails and phone numbers never reach the provider; they're put back below.
        message, pii = prompt_safety.mask_pii(message)
        if engine == "anthropic":
            text, model = _call_anthropic(engine_config, message)
        elif engine == "openai_compatible":
            text, model = _call_openai_compatible(engine_config, message)
        else:
            raise AIWriterError(f"unknown AI engine '{engine}'")
        new_subject, new_body, tone = _parse(prompt_safety.unmask_pii(text, pii))
        added_links = set(URL_RE.findall(new_subject + " " + new_body)) - set(URL_RE.findall(subject + " " + body))
        if added_links:
            raise AIWriterError("the model's version added a link that wasn't in your draft")
    except AIWriterError as e:
        result["note"] = f"your draft is used as written (AI tailoring unavailable: {e})"
        return result
    except Exception as e:  # defensive: never block a reminder
        result["note"] = f"your draft is used as written (AI tailoring failed: {e})"
        return result
    result.update(subject=new_subject, body=new_body, ai_used=True, model=model, tone=tone,
                  note=f"AI-tailored for {designation or 'an unknown designation'}" + (f" ({tone})" if tone else ""))
    return result
