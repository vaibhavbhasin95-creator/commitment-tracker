"""
Transcript -> commitment extraction, used by app.py to auto-populate
commitments the moment a transcript comes in (manual upload, or a real/
simulated live capture webhook).

The turn/utterance splitting and owner/task/due-date heuristics are lifted
directly from the original Colab prototype (Meeting_Transcript_Definition.py,
section 3). The ACTION/DIALOG classifier is the model trained by
ml/train_action_detector.py from the same script's section 2 — this module
only loads and runs it, it never trains.

The embedder and Keras model are heavy to load, so they're loaded lazily and
cached at module level: the first call pays the cost, every call after is
fast. If ml/action_detector.keras hasn't been trained yet, ModelNotReadyError
is raised so callers (app.py) can degrade gracefully instead of 500ing.
"""

import re
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

ML_DIR = Path(__file__).resolve().parent.parent / "ml"   # model artifacts live in the project root's ml/
MODEL_PATH = ML_DIR / "action_detector.keras"
EMBEDDING_MODEL_NAME = "all-MiniLM-L6-v2"
DEFAULT_THRESHOLD = 0.90

HEADER = re.compile(r"^\s*[\[(]?\d{1,2}:\d{2}(?::\d{2})?[\])]?\s*[-–—:]\s*([^:]{2,40}?)\s*$")
# Header-on-its-own-line, but with an AM/PM marker inside the brackets and no
# dash/colon separator before the name — e.g. "[10:00 AM] Bhuvan:" (common
# Otter/Teams/Zoom export shape). HEADER above doesn't match this: it expects
# the bracket to close immediately after the digits, with an explicit -/:
# separator before the name.
TS_HEADER = re.compile(
    r"^\s*[\[(]?\d{1,2}:\d{2}(?::\d{2})?\s*[AaPp]\.?[Mm]\.?[\])]?\s*[-–—:]?\s*"
    r"([A-Z][\w.'-]*(?:\s+[A-Z][\w.'-]*){0,3})\s*:\s*$"
)
INLINE = re.compile(r"^\s*([A-Z][\w.'-]*(?:\s+[A-Z][\w.'-]*){0,3})\s*:\s+(\S.*)$")
# Timestamp, speaker and words on one line — the most common export shape:
# "[09:00] Maya Singh: Good morning", "10:02 AM Kevin: ...", and WebVTT cues
# with the speaker on the timing line "00:12:31.020 --> 00:12:31.710 Rahul: ...".
TS_INLINE = re.compile(
    r"^\s*[\[(]?\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d+)?(?:\s*[AaPp]\.?[Mm]\.?)?[\])]?"
    r"(?:\s*-->\s*[\[(]?\d{1,2}:\d{2}(?::\d{2})?(?:[.,]\d+)?[\])]?)?"
    r"\s*[-–—]?\s*([A-Z][\w.'-]*(?:\s+[A-Z][\w.'-]*){0,3})\s*:\s+(\S.*)$"
)
# WebVTT/SRT scaffolding lines carry no words: cue numbers and timing lines.
CUE_SCAFFOLD = re.compile(
    r"^\s*(?:WEBVTT.*|\d+|\d{1,2}:\d{2}:\d{2}[.,]\d+\s*-->\s*\d{1,2}:\d{2}:\d{2}[.,]\d+)\s*$"
)
# An all-caps section heading inside the document ("ACTION ITEMS", "OPEN
# QUESTIONS") — what follows is written notes, not the last speaker's words.
SECTION_HEADING = re.compile(r"^\s*[A-Z][A-Z &/-]{3,}:?\s*$")
# Common transcript-preamble field labels ("Date: ...", "Participants: ...")
# that otherwise match INLINE's "Name: text" shape and would be mistaken for
# a speaker turn — dropped outright rather than treated as dialogue.
METADATA_LABELS = {
    "meeting transcript", "date", "time", "location", "participants",
    "attendees", "duration", "subject", "topic", "organizer", "agenda",
    "meeting id", "meeting", "meeting type", "format", "facilitator", "meeting lead",
    "purpose", "context", "business context", "speakers", "note", "source note",
}
# Word processors and Zoom write curly quotes ("I’ll"); the regexes below
# expect plain ASCII apostrophes.
_TYPOGRAPHY = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', " ": " "})


def normalize_text(text):
    return (text or "").translate(_TYPOGRAPHY)
SELF = re.compile(r"\b(i'?ll|i will|i can|let me|i'?m going to)\b", re.I)
OTHER = re.compile(r"\b([A-Z][a-z]+)\s+(?:will|to|should|needs? to|can|owns)\b")
YOU = re.compile(r"\b(can you|could you|please|you need to)\b", re.I)
CUE = re.compile(r"^.*?\b(?:i'?ll|we'?ll|will|should|needs? to|have to|let me|please|going to)\b\s*", re.I)
DUE = re.compile(r"\b(?:by|before|on)\s+([^,.;]{2,30})|\b(today|tomorrow|this week|next week|eod|asap)\b", re.I)
# ASR text has no punctuation, so spaCy returns 200-word "sentences" — split them
SPLIT_AT = re.compile(r"\s+(?=(?:so|and then|but|because|okay|now|then|also)\b)", re.I)

WEEKDAYS = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"]


class ModelNotReadyError(RuntimeError):
    """Raised when action_detector.keras hasn't been trained yet (see ml/train_action_detector.py)."""


_nlp = None
_embedder = None
_model = None


def _get_nlp():
    global _nlp
    if _nlp is None:
        import spacy
        _nlp = spacy.load("en_core_web_sm")
    return _nlp


def _get_embedder():
    global _embedder
    if _embedder is None:
        from sentence_transformers import SentenceTransformer
        _embedder = SentenceTransformer(EMBEDDING_MODEL_NAME)
    return _embedder


def _get_model():
    global _model
    if _model is None:
        if not MODEL_PATH.exists():
            raise ModelNotReadyError(
                f"{MODEL_PATH} not found — run `python ml/train_action_detector.py` first."
            )
        import tensorflow as tf
        _model = tf.keras.models.load_model(MODEL_PATH)
    return _model


def to_turns(raw):
    """Handles '0:00 - Name' and '[10:00 AM] Name:' header-only lines,
    '[09:00] Name: text' and WebVTT cues with the speaker on the line, and
    inline 'Name: text' on one line."""
    rows, cur = [], None
    for line in (l for l in normalize_text(raw).splitlines() if l.strip()):
        if CUE_SCAFFOLD.match(line):
            continue
        if SECTION_HEADING.match(line):
            cur = {"speaker": "UNKNOWN", "text": ""}  # written notes follow, not speech
            rows.append(cur)
            continue
        head = HEADER.match(line) or TS_HEADER.match(line)
        inline = (TS_INLINE.match(line) or INLINE.match(line)) if not head else None
        if inline and inline.group(1).strip().lower() in METADATA_LABELS:
            continue  # transcript-preamble field, not a real speaker turn
        if head:
            cur = {"speaker": head.group(1).strip(), "text": ""}
            rows.append(cur)
        elif inline:
            cur = {"speaker": inline.group(1), "text": inline.group(2)}
            rows.append(cur)
        else:
            if cur is None:
                cur = {"speaker": "UNKNOWN", "text": ""}
                rows.append(cur)
            cur["text"] = f"{cur['text']} {line.strip()}".strip()
    df = pd.DataFrame(rows, columns=["speaker", "text"])
    if df.empty:
        return df
    df.speaker = df.speaker.str.replace(r"(?i).*unidentified.*", "UNKNOWN", regex=True)
    # A HEADER line whose captured name is just digits (e.g. a stray
    # timestamp fragment that slipped past the regex) isn't a real speaker.
    df.loc[df.speaker.str.match(r"^\d+(\.\d+)?$"), "speaker"] = "UNKNOWN"
    return df[df.text.str.len() > 0].reset_index(drop=True)


def to_utterances(turns):
    nlp = _get_nlp()
    rows = []
    for _, t in turns.iterrows():
        for sent in nlp(t.text).sents:
            chunks = SPLIT_AT.split(sent.text) if len(sent.text.split()) > 40 else [sent.text]
            rows += [{"speaker": t.speaker, "text": c.strip()}
                     for c in chunks if len(c.split()) >= 4]
    return pd.DataFrame(rows, columns=["speaker", "text"])


def owner_of(text, speaker, roster):
    other = OTHER.search(text)
    if other and any(other.group(1).lower() in r.lower() for r in roster):
        return other.group(1)
    if SELF.search(text):
        return speaker
    if YOU.search(text):
        return "UNRESOLVED"
    return speaker


def task_of(text):
    body = CUE.sub("", text.strip(), count=1) or text
    return body.strip(" .,:;-")[:120]


def due_of(text):
    m = DUE.search(text)
    return (m.group(1) or m.group(2)).strip() if m else None


def resolve_due_date(due_phrase, meeting_date):
    """Best-effort mapping of a free-text due phrase ('Thursday', 'eod',
    'next week', ...) onto an ISO date relative to the meeting date. Returns
    None when the phrase doesn't resolve to anything concrete — the caller
    is expected to leave due_date unset in that case rather than guess."""
    if not due_phrase:
        return None
    phrase = due_phrase.strip().lower()
    base = meeting_date if isinstance(meeting_date, date) else date.fromisoformat(meeting_date)

    if phrase in ("today", "eod", "asap"):
        return base.isoformat()
    if phrase == "tomorrow":
        return (base + timedelta(days=1)).isoformat()
    if phrase == "this week":
        days_to_friday = (4 - base.weekday()) % 7
        return (base + timedelta(days=days_to_friday)).isoformat()
    if phrase == "next week":
        return (base + timedelta(days=7 - base.weekday())).isoformat()

    for i, day_name in enumerate(WEEKDAYS):
        if day_name in phrase:
            delta = (i - base.weekday()) % 7
            delta = delta or 7  # "Thursday" said on a Thursday means next Thursday, not today
            return (base + timedelta(days=delta)).isoformat()

    return None


def extract_commitments(transcript_text, meeting_date=None, threshold=DEFAULT_THRESHOLD):
    """Runs the full pipeline (turns -> utterances -> ACTION classification
    -> owner/task/due heuristics) over a raw transcript string, splitting it
    into speaker turns with the HEADER/TS_HEADER/INLINE regexes.

    Returns a list of dicts shaped for commitments table inserts:
    {task_text, owner_name, due_date, confidence_score, source_quote}.
    Raises ModelNotReadyError if the classifier hasn't been trained yet.
    """
    turns = to_turns(transcript_text or "")
    return extract_commitments_from_turns(turns, meeting_date=meeting_date, threshold=threshold)


def extract_commitments_from_turns(turns, meeting_date=None, threshold=DEFAULT_THRESHOLD):
    """Same pipeline as extract_commitments, but starting from turns that are
    already split into speaker/text pairs (a list of {"speaker","text"} dicts,
    or a DataFrame with those columns) — used for sources that hand us
    structured speaker turns directly (e.g. Fathom's webhook payload), where
    running the raw-text regex parser would just be re-deriving what the
    source already told us, with more room to get it wrong."""
    meeting_date = meeting_date or date.today().isoformat()

    if not isinstance(turns, pd.DataFrame):
        turns = pd.DataFrame(list(turns), columns=["speaker", "text"])
    if turns.empty:
        return []

    utts = to_utterances(turns)
    if utts.empty:
        return []

    embedder = _get_embedder()
    model = _get_model()

    embeddings = embedder.encode(
        (utts.speaker + ": " + utts.text).tolist(), batch_size=128,
        convert_to_numpy=True, normalize_embeddings=True,
    )
    utts = utts.copy()
    utts["prob"] = model.predict(embeddings, verbose=0).ravel()

    roster = set(turns.speaker) - {"UNKNOWN"}
    actions = utts[utts.prob >= threshold].copy()

    results = []
    for _, row in actions.iterrows():
        due_phrase = due_of(row.text)
        results.append({
            "task_text": task_of(row.text),
            "owner_name": owner_of(row.text, row.speaker, roster),
            "due_date": resolve_due_date(due_phrase, meeting_date),
            "confidence_score": float(row.prob),
            "source_quote": row.text,
        })
    return results
