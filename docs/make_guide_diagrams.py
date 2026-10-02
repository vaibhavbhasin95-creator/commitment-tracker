"""
Draws the diagrams used in docs/Commitment_Tracker_Technical_Guide.docx.

Each diagram is written as an SVG (docs/guide_diagrams/*.svg), then rendered to
a PNG with headless Microsoft Edge so Word can embed it. Diagrams are drawn on
a 1600 px wide canvas with 18-20 px text, sized for a landscape A4 page.

Run:  python docs/make_guide_diagrams.py            # all diagrams
      python docs/make_guide_diagrams.py er arch    # only these (name substring)
"""

import subprocess
import sys
import tempfile
import textwrap
import time
from html import escape
from pathlib import Path

OUT = Path(__file__).resolve().parent / "guide_diagrams"
EDGE_CANDIDATES = [
    Path(r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe"),
    Path(r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"),
]
FONT = "Segoe UI, Arial, sans-serif"
MONO = "Consolas, 'Courier New', monospace"
INK = "#1A202C"
MUTED = "#4A5568"
PAL = {  # kind: (fill, stroke)
    "ui": ("#E8F1FB", "#2B6CB0"),
    "app": ("#E9F7EF", "#2F855A"),
    "mod": ("#E6FFFA", "#2C7A7B"),
    "ml": ("#F3EEFC", "#6B46C1"),
    "ext": ("#FEF3E6", "#C05621"),
    "db": ("#EEF1F4", "#4A5568"),
    "risk": ("#FDECEC", "#C53030"),
    "note": ("#FFFBEA", "#B7791F"),
    "ok": ("#EAF7EE", "#276749"),
    "plain": ("#FFFFFF", "#A0AEC0"),
}
DIAGRAMS = {}


def diagram(name, w, h):
    def register(fn):
        DIAGRAMS[name] = (fn, w, h)
        return fn
    return register


# ---------------------------------------------------------------- primitives

class Canvas:
    def __init__(self, w, h):
        self.w, self.h, self.parts = w, h, []

    def add(self, s):
        self.parts.append(s)

    def svg(self):
        return (f'<svg xmlns="http://www.w3.org/2000/svg" width="{self.w}" height="{self.h}" '
                f'viewBox="0 0 {self.w} {self.h}" font-family="{FONT}">'
                '<defs>'
                + "".join(
                    f'<marker id="ah{n}" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="9" markerHeight="9" '
                    f'orient="auto-start-reverse"><path d="M0,0 L10,5 L0,10 z" fill="{c}"/></marker>'
                    for n, c in ARROW_COLORS.items())
                + '</defs>'
                f'<rect width="{self.w}" height="{self.h}" fill="#FFFFFF"/>'
                + "".join(self.parts) + "</svg>")


ARROW_COLORS = {"": MUTED, "o": "#C05621", "r": "#C53030", "g": "#2F855A", "b": "#2B6CB0", "p": "#6B46C1"}


def wrap(text, width, size, mono=False):
    per_char = size * (0.55 if mono else 0.52)
    out = []
    for para in str(text).split("\n"):
        out += textwrap.wrap(para, max(4, int(width / per_char))) or [""]
    return out


def text(c, x, y, s, size=18, weight="normal", color=INK, anchor="start", mono=False, italic=False):
    c.add(f'<text x="{x}" y="{y}" font-size="{size}" font-weight="{weight}" fill="{color}" '
          f'text-anchor="{anchor}"{" font-style=%sitalic%s" % (chr(34), chr(34)) if italic else ""}'
          f'{(" font-family=" + chr(34) + MONO + chr(34)) if mono else ""}>{escape(s)}</text>')


def box(c, x, y, w, title=None, lines=(), kind="app", h=None, size=18, tsize=20, align="middle",
        mono=False, dashed=False, title_color=None, radius=10):
    """Rounded box with a bold title and wrapped body lines; height grows to fit
    unless h is given. Returns (x, y, w, h)."""
    fill, stroke = PAL[kind]
    tl = wrap(title, w - 24, tsize) if title else []
    bl = []
    for line in lines:
        bl += wrap(line, w - 28, size, mono)
    content = len(tl) * (tsize + 6) + len(bl) * (size + 6) + (4 if tl and bl else 0)
    h = h or content + 26
    dash = ' stroke-dasharray="7 5"' if dashed else ""
    c.add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="{radius}" fill="{fill}" stroke="{stroke}" '
          f'stroke-width="2"{dash}/>')
    cy = y + (h - content) / 2
    tx, anchor = (x + w / 2, "middle") if align == "middle" else (x + 14, "start")
    for t in tl:
        cy += tsize
        text(c, tx, cy - 3, t, tsize, "bold", title_color or stroke, anchor)
        cy += 6
    if tl and bl:
        cy += 4
    for t in bl:
        cy += size
        text(c, tx, cy - 3, t, size, color=INK, anchor=anchor, mono=mono)
        cy += 6
    return (x, y, w, h)


def group(c, x, y, w, h, label, kind="plain", size=19):
    fill, stroke = PAL[kind]
    c.add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="14" fill="{fill}" fill-opacity="0.55" '
          f'stroke="{stroke}" stroke-width="2" stroke-dasharray="9 6"/>')
    text(c, x + 14, y + size + 6, label, size, "bold", stroke)


def cylinder(c, x, y, w, h, title, lines=(), kind="db"):
    fill, stroke = PAL[kind]
    e = 16
    c.add(f'<path d="M{x},{y + e} a{w / 2},{e} 0 0,0 {w},0 v{h - 2 * e} a{w / 2},{e} 0 0,1 {-w},0 z" '
          f'fill="{fill}" stroke="{stroke}" stroke-width="2"/>'
          f'<ellipse cx="{x + w / 2}" cy="{y + e}" rx="{w / 2}" ry="{e}" fill="{fill}" stroke="{stroke}" stroke-width="2"/>')
    cy = y + 2 * e + 26
    text(c, x + w / 2, cy, title, 20, "bold", stroke, "middle")
    for line in lines:
        for t in wrap(line, w - 30, 17):
            cy += 23
            text(c, x + w / 2, cy, t, 17, anchor="middle")
    return (x, y, w, h)


def diamond(c, cx, cy, w, h, label, kind="note", size=17):
    fill, stroke = PAL[kind]
    c.add(f'<polygon points="{cx},{cy - h / 2} {cx + w / 2},{cy} {cx},{cy + h / 2} {cx - w / 2},{cy}" '
          f'fill="{fill}" stroke="{stroke}" stroke-width="2"/>')
    lines = wrap(label, w * 0.62, size)
    y0 = cy - (len(lines) * (size + 4)) / 2 + size - 2
    for i, t in enumerate(lines):
        text(c, cx, y0 + i * (size + 4), t, size, "bold" if len(lines) == 1 else "normal", INK, "middle")
    return (cx - w / 2, cy - h / 2, w, h)


def label_at(c, x, y, s, size=16, color=MUTED, bg="#FFFFFF", anchor="middle", weight="normal"):
    lines = s.split("\n")
    wpx = max(len(t) for t in lines) * size * 0.53 + 14
    hpx = len(lines) * (size + 4) + 6
    bx = x - wpx / 2 if anchor == "middle" else (x if anchor == "start" else x - wpx)
    c.add(f'<rect x="{bx}" y="{y - hpx / 2}" width="{wpx}" height="{hpx}" rx="5" fill="{bg}" fill-opacity="0.94"/>')
    for i, t in enumerate(lines):
        tx = x if anchor == "middle" else (bx + 7 if anchor == "start" else x - 7)
        text(c, tx, y - hpx / 2 + 3 + (i + 1) * (size + 4) - 4, t, size, weight, color, anchor)


def arrow(c, pts, label=None, at=None, color="", dashed=False, both=False, width=2.2, lsize=16, lanchor="middle"):
    stroke = ARROW_COLORS[color]
    d = " ".join(f"{'M' if i == 0 else 'L'}{x},{y}" for i, (x, y) in enumerate(pts))
    dash = ' stroke-dasharray="8 6"' if dashed else ""
    start = f' marker-start="url(#ah{color})"' if both else ""
    c.add(f'<path d="{d}" fill="none" stroke="{stroke}" stroke-width="{width}"{dash}{start} '
          f'marker-end="url(#ah{color})"/>')
    if label:
        if at is None:
            (x1, y1), (x2, y2) = pts[len(pts) // 2 - 1], pts[len(pts) // 2]
            at = ((x1 + x2) / 2, (y1 + y2) / 2)
        label_at(c, at[0], at[1], label, lsize, stroke if color else MUTED, anchor=lanchor)


def R(b, dy=0):
    return (b[0] + b[2], b[1] + b[3] / 2 + dy)


def L(b, dy=0):
    return (b[0], b[1] + b[3] / 2 + dy)


def T(b, dx=0):
    return (b[0] + b[2] / 2 + dx, b[1])


def B(b, dx=0):
    return (b[0] + b[2] / 2 + dx, b[1] + b[3])


def legend(c, x, y, items, size=17):
    for kind, name in items:
        fill, stroke = PAL[kind]
        c.add(f'<rect x="{x}" y="{y - 15}" width="26" height="20" rx="4" fill="{fill}" stroke="{stroke}" stroke-width="2"/>')
        text(c, x + 34, y, name, size, color=MUTED)
        x += 34 + len(name) * size * 0.52 + 30


# ---------------------------------------------------------------- diagrams

@diagram("01_architecture", 1600, 1000)
def d_architecture(c):
    group(c, 12, 12, 360, 650, "Browser — static/ (HTML + vanilla JS)", "ui")
    pages = [
        ("index.html", "Sign in with an email (no password)"),
        ("dashboard.html", "Meetings, tasks, upload, status/owner edits"),
        ("people.html", "People directory CSV, owner fixes"),
        ("settings.html", "Nudge channels, tracker, transcript source"),
        ("ai-extraction.html", "Choose engine, compare, scoreboard"),
        ("app.js + style.css", "api(), escapeHtml(), toast(), theme"),
    ]
    y = 62
    for t, d in pages:
        b = box(c, 30, y, 324, t, [d], "ui", size=16, tsize=18)
        y += b[3] + 12

    off = box(c, 12, 680, 360, "Offline scripts (never run by the app)",
              ["ml/train_action_detector.py", "ml/retrain_with_transcripts.py", "google_oauth_setup.py (one-time)",
               "tests/run_all.py (119 checks)"], "db", size=16, tsize=18, align="start")

    group(c, 420, 12, 420, 900, "Flask app — app.py (one process)", "app")
    blocks = [
        ("Startup & DB helpers", "get_db · init_db · migrate_db"),
        ("Sign-in", "/api/identify · /api/me · /api/logout"),
        ("Meetings & extraction", "upload · auto_extract_commitments"),
        ("Commitments", "list · PATCH status/owner · add by hand"),
        ("People directory", "CSV upload · rematch_owners"),
        ("Nudges", "channels · run now · scheduler thread"),
        ("Tracker sync", "Google Sheets · Jira (mock) · webhook"),
        ("Transcript sources", "Fathom + generic capture webhooks"),
        ("AI extraction (optional)", "routes from ai_extraction.init_app"),
        ("Setup-steps helper", "Claude-written how-to, or template"),
    ]
    y, app_boxes = 60, []
    for t, d in blocks:
        b = box(c, 440, y, 380, t, [d], "app", size=16, tsize=18)
        app_boxes.append(b)
        y += b[3] + 10

    mods = {}
    mods["keras"] = box(c, 905, 30, 300, "ml/action_detector.keras", ["trained classifier weights"], "db", size=16, tsize=18)
    mods["tp"] = box(c, 905, 150, 300, "transcript_parser.py", ["turns → sentences → embeddings → MLP", "owner / task / due rules"], "ml", size=16, tsize=18)
    mods["ai"] = box(c, 905, 300, 300, "ai_extraction.py", ["Claude or OpenAI-compatible", "falls back to built-in"], "ml", size=16, tsize=18)
    mods["pd"] = box(c, 905, 425, 300, "people_directory.py", ["parse_csv · match_owner"], "mod", size=16, tsize=18)
    mods["nt"] = box(c, 905, 525, 300, "notifications.py", ["email · Slack · Teams · custom"], "mod", size=16, tsize=18)
    mods["gs"] = box(c, 905, 625, 300, "google_sheets.py", ["OAuth token · values.get / update"], "mod", size=16, tsize=18)
    db = cylinder(c, 905, 765, 300, 150, "commitment_tracker.db", ["SQLite · 14 tables", "incl. channel secrets"])

    ext = {}
    ext["hf"] = box(c, 1270, 30, 315, "Hugging Face hub", ["MiniLM model download (first run)"], "ext", size=16, tsize=18)
    ext["llm"] = box(c, 1270, 150, 315, "Anthropic / OpenAI-compatible", ["e.g. Gemini 2.5 Pro, with the account's key"], "ext", size=16, tsize=18)
    ext["sheets"] = box(c, 1270, 290, 300, "Google Sheets API", ["+ Apps Script onStatusEdit trigger"], "ext", size=16, tsize=18)
    ext["msg"] = box(c, 1270, 410, 315, "SMTP · Slack · Teams · URL", ["reminder delivery"], "ext", size=16, tsize=18)
    ext["fathom"] = box(c, 1270, 520, 300, "Fathom", ["'meeting content ready' webhook"], "ext", size=16, tsize=18)
    ext["ngrok"] = box(c, 1270, 630, 300, "ngrok tunnel", ["public https URL → localhost:5000"], "risk", size=16, tsize=18)
    ext["files"] = box(c, 1270, 770, 315, "Credential files (gitignored)", ["client_secret_*.json", "google_token.json"], "db", size=16, tsize=18)

    arrow(c, [(372, 300), (420, 300)], color="b", both=True)
    label_at(c, 396, 268, "JSON /api/*\n+ session\ncookie", 15, "#2B6CB0")
    # app -> modules (bus at x=865)
    me = app_boxes[2]
    arrow(c, [R(me), (865, R(me)[1]), (865, L(mods["tp"])[1]), L(mods["tp"])])
    arrow(c, [(865, R(me)[1]), (865, L(mods["ai"])[1]), L(mods["ai"])])
    arrow(c, [R(app_boxes[4]), (880, R(app_boxes[4])[1]), (880, L(mods["pd"])[1]), L(mods["pd"])])
    arrow(c, [R(app_boxes[5]), (880, R(app_boxes[5])[1]), (880, L(mods["nt"])[1]), L(mods["nt"])])
    arrow(c, [R(app_boxes[6]), (880, R(app_boxes[6])[1]), (880, L(mods["gs"])[1]), L(mods["gs"])])
    arrow(c, [(840, 860), (905, 860)], "SQL", at=(872, 840), lsize=15)
    arrow(c, [B(mods["keras"]), T(mods["tp"])], "loaded once", at=(1110, 135), lsize=15)
    # modules -> external
    arrow(c, [R(mods["tp"], -10), (1240, R(mods["tp"], -10)[1]), (1240, L(ext["hf"])[1]), L(ext["hf"])], color="o", dashed=True)
    arrow(c, [R(mods["ai"]), (1240, R(mods["ai"])[1]), (1240, L(ext["llm"], 12)[1]), L(ext["llm"], 12)], color="o")
    arrow(c, [R(mods["gs"], -12), (1225, R(mods["gs"], -12)[1]), (1225, L(ext["sheets"])[1]), L(ext["sheets"])], color="o")
    arrow(c, [R(mods["nt"]), (1255, R(mods["nt"])[1]), (1255, L(ext["msg"])[1]), L(ext["msg"])], color="o")
    arrow(c, [R(mods["gs"], 14), (1245, R(mods["gs"], 14)[1]), (1245, L(ext["files"])[1]), L(ext["files"])], dashed=True)
    # inbound webhooks
    arrow(c, [R(ext["sheets"]), (1588, R(ext["sheets"])[1]), (1588, R(ext["ngrok"], -10)[1]), R(ext["ngrok"], -10)], color="r", dashed=True)
    arrow(c, [B(ext["fathom"]), T(ext["ngrok"])], color="r", dashed=True)
    tgt = app_boxes[7]
    arrow(c, [L(ext["ngrok"], 20), (1225, L(ext["ngrok"], 20)[1]), (1225, 745), (860, 745), (860, R(tgt)[1]), R(tgt)],
          "inbound webhooks", at=(1040, 745), color="r", dashed=True)

    legend(c, 30, 975, [("ui", "Browser"), ("app", "Flask routes"), ("ml", "ML / AI"), ("mod", "Helper module"),
                        ("ext", "External service"), ("db", "Storage / files"), ("risk", "Public exposure")])


@diagram("02_upload_sequence", 1600, 1000)
def d_upload_sequence(c):
    lanes = [("Browser", "dashboard.html", "ui"), ("app.py", "create_meeting", "app"),
             ("ai_extraction", "try_extract", "ml"), ("transcript_parser", "extract_commitments", "ml"),
             ("people_directory", "match_owner", "mod"), ("SQLite", "commitment_tracker.db", "db"),
             ("Google Sheets", "API v4", "ext")]
    xs = [105 + i * 232 for i in range(len(lanes))]
    for x, (t, s, k) in zip(xs, lanes):
        box(c, x - 105, 10, 210, t, [s], k, size=15, tsize=18)
        c.add(f'<line x1="{x}" y1="78" x2="{x}" y2="940" stroke="#A0AEC0" stroke-width="2" stroke-dasharray="6 6"/>')
    B_, A, AI, TP, PD, DB, GS = xs
    y = [0]

    def msg(a, b, label, dy=44, color="", dashed=False):
        y[0] += dy
        yy = 80 + y[0]
        if a == b:
            arrow(c, [(a, yy - 10), (a + 40, yy - 10), (a + 40, yy + 8), (a + 4, yy + 8)], color=color)
            label_at(c, a + 48, yy - 1, label, 16, MUTED, anchor="start")
        else:
            arrow(c, [(a, yy), (b, yy)], color=color, dashed=dashed)
            if b < a and b == xs[0]:
                label_at(c, b + 8, yy - 14, label, 16, ARROW_COLORS[color] if color else INK, anchor="start")
            else:
                label_at(c, (a + b) / 2, yy - 14, label, 16, ARROW_COLORS[color] if color else INK)

    msg(B_, A, "1  POST /api/meetings {title, date, transcript}", 20, "b")
    msg(A, A, "2  require_email() — no session → 401")
    msg(A, A, "3  looks_like_binary_text() — .docx bytes → 400")
    msg(A, DB, "4  INSERT meetings (write transaction opens)")
    msg(A, AI, "5  try_extract()", color="p")
    y0 = 80 + y[0] - 26
    msg(AI, DB, "6  read ai_extraction_settings")
    msg(AI, A, "7  None → use built-in   (or: COMMIT, call LLM, return items)", color="p", dashed=True)
    c.add(f'<rect x="{A - 20}" y="{y0}" width="{DB - A + 60}" height="{80 + y[0] - y0 + 16}" rx="8" fill="none" '
          f'stroke="#6B46C1" stroke-width="1.6" stroke-dasharray="4 4"/>')
    label_at(c, DB - 60, y0 + 2, "alt: AI engine chosen", 14, "#6B46C1", "#F3EEFC")
    msg(A, TP, "8  extract_commitments(text, meeting_date)", 52, "p")
    msg(TP, TP, "9  turns → sentences → embed → MLP ≥ 0.90 → rules")
    msg(TP, A, "10  [{task_text, owner_name, due_date, confidence, quote}]", color="p", dashed=True)
    msg(A, PD, "11  match_owner(owner_name, people)")
    msg(PD, A, "12  (email | None, auto | ambiguous | none)", dashed=True)
    msg(A, DB, "13  INSERT commitments (skip quotes already saved)")
    msg(A, DB, "14  COMMIT · close")
    msg(A, A, "15  auto_sync_to_sheet() → BEGIN IMMEDIATE")
    msg(A, GS, "16  values.get A:A  ·  values.update A{n}:E{m}", color="o")
    msg(A, DB, "17  UPDATE external_ref = 'row-n' · tracker_sync_log · COMMIT")
    msg(A, B_, "18  201 {id, commitments_extracted, extraction_error, synced_to_tracker}", 50, "b", True)
    box(c, 830, 930, 760, None, ["Extraction or sync failures never undo the upload: they come back as "
                                 "extraction_error / tracker_sync_error and the meeting is kept."], "note", size=16)


@diagram("03_extraction_pipeline", 1600, 850)
def d_pipeline(c):
    stages = [
        ("① Raw transcript", ["upload .txt, capture webhook text, or Fathom turns"], "ui"),
        ("② normalize_text", ["curly quotes ’ “ ” → ' \"", "non-breaking space → space"], "mod"),
        ("③ to_turns", ["each line: timestamp header, inline 'Name: text', scaffolding, heading, or continuation",
                        "→ (speaker, text) turns"], "mod"),
        ("④ to_utterances", ["spaCy sentence split", "> 40 words: split before so / but / and then …",
                             "drop pieces < 4 words"], "mod"),
        ("⑤ Embed", ["'speaker: text'", "MiniLM-L6-v2 → 384 numbers", "batch 128, length-normalised"], "ml"),
        ("⑥ Classify", ["MLP 384→256→128→1", "sigmoid = P(commitment)", "keep if ≥ 0.90"], "ml"),
    ]
    boxes = []
    for i, (t, ls, k) in enumerate(stages):
        boxes.append(box(c, 14 + i * 264, 14, 240, t, ls, k, h=230, size=16, tsize=19))
    for a, b in zip(boxes, boxes[1:]):
        arrow(c, [R(a), L(b)])
    rules = [
        ("⑦ owner_of", ["'Kevin will…' → Kevin (if he spoke)", "I'll / I will / let me → speaker",
                        "can you / please → UNRESOLVED"]),
        ("⑧ task_of", ["drop words up to the first cue (I'll, will, need to, please …)", "cut at 120 characters"]),
        ("⑨ due_of + resolve_due_date", ["first 'by / before / on X' or today, tomorrow, eod …",
                                         "weekday or relative → ISO date, else None"]),
        ("⑩ match_owner", ["email / username → full name → first or last name", "auto · ambiguous · none"]),
        ("⑪ INSERT commitments", ["status pending + confidence + source_quote", "skip a quote already saved for the meeting"]),
    ]
    rb = []
    for i, (t, ls) in enumerate(rules):
        rb.append(box(c, 14 + i * 316, 300, 292, t, ls, "app" if i < 3 else ("mod" if i == 3 else "db"), h=170, size=16, tsize=19))
    arrow(c, [B(boxes[-1]), (B(boxes[-1])[0], 272), (B(rb[0])[0], 272), T(rb[0])], "for each kept sentence",
          at=(800, 272), lsize=16)
    for a, b in zip(rb, rb[1:]):
        arrow(c, [R(a), L(b)])

    group(c, 14, 500, 1572, 340, "Worked example — real run of the production model on two lines of the FY2027 transcript", "note")
    col = [30, 545, 1075]
    box(c, col[0], 548, 495, "Input (TS_HEADER lines + text)", [
        "[11:28 AM] Sarah Xian:",
        "Understood, Bhuvan. I’ll track everyone’s deliverables from the 26th through the 29th, cross-reference the "
        "numbers with HR leadership, and give you the master feedback report on the 30th.",
        "[11:29 AM] Bhuvan:",
        "Once I receive Sarah’s feedback on the 30th, I will immediately issue a Calendar invite by Friday."],
        "plain", size=15, tsize=17, align="start", mono=True, title_color=INK)
    box(c, col[1], 548, 510, "③ → ⑥ what each stage produced", [
        "③ 2 turns: 'Sarah Xian' and 'Bhuvan' (curly ’ already turned into ').",
        "④ spaCy splits Sarah's turn in two; 'Understood, Bhuvan.' has 2 words (< 4) so it is dropped. "
        "2 sentences remain.",
        "⑤ a 2 × 384 matrix (first values −0.062, −0.085, −0.016, 0.031 …).",
        "⑥ probabilities 1.0000 and 0.9999, both ≥ 0.90 → both kept."],
        "plain", size=16, tsize=17, align="start", title_color=INK)
    box(c, col[2], 548, 495, "⑦ → ⑨ rules, and what to learn from them", [
        "#1 owner Sarah Xian (I'll → speaker). Task cut at 120 chars: '…and give y'.",
        "#2 owner Bhuvan (I will → speaker). Task 'immediately issue a Calendar invite by Friday'.",
        "Both due dates are None: the first match is 'on the 30th', and resolve_due_date only knows "
        "weekdays and words like tomorrow — so 'by Friday' is never looked at.",
        "→ Known limits: mid-word cut, first due phrase wins, calendar dates not parsed."],
        "risk", size=16, tsize=17, align="start")


@diagram("04_database_er", 1600, 900)
def d_er(c):
    def table(x, y, w, name, cols, kind="db"):
        fill, stroke = PAL[kind]
        h = 34 + len(cols) * 23 + 8
        c.add(f'<rect x="{x}" y="{y}" width="{w}" height="{h}" rx="8" fill="#FFFFFF" stroke="{stroke}" stroke-width="2"/>'
              f'<path d="M{x},{y + 8} a8,8 0 0,1 8,-8 h{w - 16} a8,8 0 0,1 8,8 v26 h{-w} z" fill="{stroke}"/>')
        text(c, x + 12, y + 24, name, 18, "bold", "#FFFFFF")
        for i, col_ in enumerate(cols):
            secret = col_.endswith("!")
            key = col_.startswith(("PK ", "FK ", "UQ "))
            label = col_.rstrip("!")
            text(c, x + 12, y + 34 + (i + 1) * 23 - 4, label + ("  🔑 secret" if secret else ""), 16,
                 "bold" if key else "normal", "#C53030" if secret else INK)
        return (x, y, w, h)

    t = {}
    t["users"] = table(14, 14, 290, "users", ["PK id", "UQ email", "created_at"])
    t["people"] = table(14, 150, 290, "people", ["PK id", "owner_email ✉", "first_name · last_name", "email",
                                                  "username", "UQ (owner_email, email)"])
    t["prefs"] = table(14, 350, 290, "nudge_preferences", ["PK user_email ✉", "auto_enabled"])
    t["nset"] = table(14, 460, 290, "nudge_settings", ["PK id", "user_email ✉ + channel (UQ)", "custom_name",
                                                       "webhook_or_key !", "cadence · enabled", "config_json (SMTP pwd) !"])
    t["meet"] = table(345, 14, 310, "meetings", ["PK id", "owner_email ✉", "title · meeting_date", "source upload|live|generated",
                                                 "transcript_excerpt (full text)", "transcript_provider", "created_at"])
    t["comm"] = table(345, 255, 310, "commitments", ["PK id", "FK meeting_id → meetings", "task_text", "owner_name",
                                                     "owner_email · owner_match", "due_date", "status pending|in_progress|done",
                                                     "confidence_score", "source_quote", "external_ref  'row-n'", "created_at"])
    t["tlog"] = table(700, 14, 290, "transcript_sync_log", ["PK id", "user_email ✉", "provider · direction",
                                                            "triggered_at · status", "detail", "FK meeting_id → meetings"])
    t["nlog"] = table(700, 250, 290, "nudge_log", ["PK id", "FK commitment_id", "triggered_at", "channel · status",
                                                  "reason · detail"])
    t["trlog"] = table(700, 430, 290, "tracker_sync_log", ["PK id", "FK commitment_id", "provider · direction",
                                                           "triggered_at · status", "detail"])
    t["tset"] = table(1030, 14, 270, "tracker_settings", ["PK id", "UQ user_email ✉", "provider · custom_name",
                                                         "config_json (sheet id) !", "enabled"])
    t["sset"] = table(1030, 200, 270, "transcript_source_settings", ["PK id", "UQ user_email ✉", "provider · custom_name",
                                                                    "config_json (whsec) !", "enabled", "UQ webhook_token !"])
    group(c, 1318, 8, 274, 652, "ai_extraction.py tables", "ml", 17)
    t["aset"] = table(1330, 44, 250, "ai_extraction_settings", ["PK user_email ✉", "engine", "config_json (API key) !",
                                                                "consent_at · updated_at"], "ml")
    t["aruns"] = table(1330, 200, 250, "ai_extraction_runs", ["PK id", "user_email ✉", "meeting_id", "engine · model",
                                                             "status ok | fell_back", "commitments · detail"], "ml")
    t["acmp"] = table(1330, 410, 250, "ai_extraction_comparisons", ["PK id", "user_email ✉", "meeting_id",
                                                                   "engine · model", "builtin_json · ai_json",
                                                                   "verdicts_json"], "ml")
    arrow(c, [T(t["comm"]), B(t["meet"])], "N : 1", at=(500, 236), color="b")
    arrow(c, [L(t["nlog"], -30), (655, L(t["nlog"], -30)[1])], color="b")
    arrow(c, [L(t["trlog"], -30), (655, L(t["trlog"], -30)[1])], color="b")
    arrow(c, [L(t["tlog"], 60), (655, L(t["tlog"], 60)[1])], color="b")
    box(c, 14, 700, 1578, "How to read this", [
        "Blue arrows are real FOREIGN KEYs (PRAGMA foreign_keys = ON in get_db). "
        "✉ marks an email column with no FK: it logically points at users.email and every query filters on it — that is "
        "how one account's data is kept apart from another's.",
        "Red 🔑 columns hold secrets in plain text (no encryption at rest). The whole file is gitignored (*.db) — protect "
        "the PC and any backup of it.",
        "meeting_id in the ai_extraction tables is not a declared FK, so deleting a meeting would leave those rows behind.",
    ], "note", size=17, tsize=19, align="start")


@diagram("05_nudge_scheduler", 1600, 840)
def d_nudges(c):
    s1 = box(c, 14, 20, 330, "python app.py", ["the debug reloader runs a second copy with WERKZEUG_RUN_MAIN=true"], "app", size=16)
    s2 = box(c, 14, 170, 330, "start_nudge_scheduler()", ["daemon thread 'nudge-scheduler'",
                                                          "interval = NUDGE_CHECK_INTERVAL_SECONDS (default 900 s = 15 min)"], "app", size=16)
    s3 = box(c, 14, 340, 330, "nudge_scheduler_loop", ["sleep(interval)", "accounts = nudge_preferences WHERE auto_enabled = 1"], "app", size=16)
    s4 = box(c, 14, 510, 330, "run_nudges(account)", ["also called by the 'Run nudges now' button"], "app", size=16)
    arrow(c, [B(s1), T(s2)])
    arrow(c, [B(s2), T(s3)])
    arrow(c, [B(s3), T(s4)], "for each account", at=(179, 488))
    arrow(c, [L(s4), (4, L(s4)[1]), (4, L(s3)[1]), L(s3)], color="g", dashed=True)
    err = box(c, 14, 660, 330, "Any exception in a pass", ["printed as '[nudges] scheduler pass failed: …' and the loop keeps going"], "risk", size=16)

    d1 = box(c, 400, 20, 400, "due_nudges(db, account)", ["settings = enabled nudge_settings rows",
                                                          "tasks = open tasks (status ≠ done) that have a due_date"], "app", size=16)
    arrow(c, [R(s4), (370, R(s4)[1]), (370, L(d1)[1]), L(d1)])
    dm1 = diamond(c, 600, 300, 400, 200, "Cadence rule matches? day_before: due = tomorrow · on_due: due = today · "
                                         "daily_after_overdue: due < today", size=16)
    arrow(c, [B(d1), (600, 200)], "each channel × each task", at=(600, 180))
    dm2 = diamond(c, 600, 560, 400, 200, "Tried in the last 20 h — or a one-off reminder already sent?", size=16)
    arrow(c, [(600, 400), (600, 460)], "yes", at=(626, 430), color="g")
    skip = box(c, 860, 420, 200, "skip", [], "plain", h=56)
    arrow(c, [(800, 300), (960, 300), T(skip)], "no", at=(880, 300))
    arrow(c, [(800, 560), (960, 560), B(skip)], "yes", at=(880, 560))
    plan = box(c, 470, 720, 260, "add to plan", ["(setting, task, reason)"], "app", size=16)
    arrow(c, [(600, 660), T(plan)], "no", at=(626, 690), color="g")

    snd = box(c, 1100, 20, 486, "send_and_log(setting, task, reason)", ["config = json.loads(config_json)"], "app", size=16)
    arrow(c, [R(plan), (1060, R(plan)[1]), (1060, L(snd)[1]), L(snd)], "for each planned item", at=(1060, 140))
    dl = box(c, 1100, 160, 486, "notifications.deliver(channel, …)", ["compose() subject + body, then one sender — "
                                                                       "never raises, returns (status, detail)"], "mod", size=16)
    arrow(c, [B(snd), T(dl)])
    ch = [("email", "SMTP · STARTTLS 587 or SSL 465 · to owner_email"),
          ("slack", "xoxb token → DM by email, or incoming webhook"),
          ("teams", "Workflows URL · Adaptive Card"),
          ("custom", "POST JSON to any URL")]
    y = 320
    for n, d in ch:
        b = box(c, 1150, y, 436, n, [d], "ext", size=16, tsize=18)
        arrow(c, [(1120, R(dl)[1] + 50), (1120, R(b)[1]), L(b)])
        y += b[3] + 10
    log = box(c, 1100, y + 20, 486, "INSERT nudge_log", ["status sent | failed | skipped, reason, detail — "
                                                          "read it first when a reminder 'didn't arrive'"], "db", size=16)
    arrow(c, [(1100, R(dl)[1]), (1080, R(dl)[1]), (1080, L(log)[1]), L(log)], "(status, detail)", at=(1080, y - 140))


@diagram("06_sheets_two_way", 1600, 940)
def d_sheets(c):
    group(c, 10, 10, 1580, 470, "Outbound — App → Google Sheet", "app")
    trig = box(c, 30, 60, 300, "Triggers", ["upload / capture / add task → auto_sync_to_sheet (needs auto_sync on)",
                                            "'Sync all' → sync_all_to_tracker", "'Sync' on a task → sync_tracker"],
               "ui", size=16, align="start")
    lock = box(c, 380, 60, 270, "BEGIN IMMEDIATE", ["take the SQLite write lock first, so two requests can't both "
                                                    "write the same rows (the old double-sync bug)"], "db", size=16)
    sel = box(c, 700, 60, 300, "Pick rows to send", ["get_sheets_tracker_config()", "commitments WHERE external_ref IS NULL"],
              "app", size=16)
    api = box(c, 1050, 60, 520, "google_sheets.append_commitment_rows", [
        "values.get A:A → next empty row n (header row first if the sheet is empty)",
        "values.update A{n}:E{m}  [ID, Task, Owner, Due date, Status]",
        "explicit range, not values.append — append shifted rows right when a due date was blank"], "mod", size=16)
    arrow(c, [R(trig, -20), L(lock, -20)])
    arrow(c, [R(lock), L(sel)])
    arrow(c, [R(sel), L(api)])
    save = box(c, 1050, 270, 520, "Save the link", ["UPDATE commitments SET external_ref = 'row-n'",
                                                   "tracker_sync_log (outbound, synced) · COMMIT"], "db", size=16)
    arrow(c, [B(api), T(save)], "row numbers", at=(1310, 250))
    st = box(c, 30, 300, 620, "Status changed in the app (PATCH /api/commitments/<id>)", [
        "auto_push_status_to_sheet → update_status(): writes only cell E{n}.",
        "An API write doesn't fire the sheet's On edit trigger, so no echo loop."], "ui", size=16, align="start")
    arrow(c, [R(st), (1000, R(st)[1]), (1000, 220), (1050, 220)], "E{n} = 'Done'", at=(830, R(st)[1]))

    group(c, 10, 500, 1580, 430, "Inbound — Google Sheet → App", "ext")
    steps = [
        ("Person edits Status (column E)", ["e.g. 'Closed'"], "ui"),
        ("Apps Script: onStatusEdit(e)", ["installable 'On edit' trigger (a simple onEdit can't call URLs)",
                                          "UrlFetchApp POST {email, external_ref: 'row-n', status}"], "ext"),
        ("ngrok public URL", ["https → localhost:5000", "free plan URL changes on restart!"], "risk"),
        ("POST /api/webhooks/tracker-update", ["find commitment by owner email + external_ref (404 if none)"], "app"),
    ]
    x, prev = 30, None
    widths = [280, 420, 330, 420]
    for (t, ls, k), w in zip(steps, widths):
        b = box(c, x, 550, w, t, ls, k, h=150, size=16)
        if prev:
            arrow(c, [R(prev), L(b)], color="o")
        prev, x = b, x + w + 30
    nrm = box(c, 1150, 740, 420, "normalize_tracker_status", ["Closed / Complete → done · WIP → in_progress · "
                                                              "unknown → 400"], "app", size=16)
    arrow(c, [B(prev), T(nrm)], color="o")
    upd = box(c, 620, 740, 470, "apply_inbound_tracker_update", ["UPDATE status · tracker_sync_log (inbound, received) · "
                                                                 "COMMIT · 201"], "db", size=16)
    arrow(c, [L(nrm), R(upd)], color="o")
    box(c, 30, 740, 540, "⚠ No secret on this webhook", ["Anyone with the ngrok URL who knows the account email can "
                                                         "change a status by guessing row-n. Backlog item: add a shared token."],
        "risk", size=16)


@diagram("07_trust_boundaries", 1600, 960)
def d_trust(c):
    group(c, 10, 10, 880, 640, "Your PC — trusted zone", "ok")
    files = box(c, 30, 60, 420, "Project folder (files)", [
        "client_secret_*.json — OAuth client id/secret",
        "google_token.json — refresh token: full Sheets access",
        "commitment_tracker.db — plain-text secrets:",
        "  SMTP password · Slack token · webhook URLs",
        "  AI API keys · Fathom whsec · webhook tokens",
        "app.py: secret_key hard-coded (signs cookies)"], "db", size=16, align="start")
    env = box(c, 30, 370, 420, "Environment variables", [
        "ANTHROPIC_API_KEY — setup-steps helper only",
        "ANTHROPIC_MODEL · AI_EXTRACTION_ENABLED",
        "NUDGE_CHECK_INTERVAL_SECONDS"], "db", size=16, align="start")
    flask = box(c, 490, 60, 380, "Flask process — localhost:5000", [
        "debug=True → Werkzeug debugger", "no login: an email is enough",
        "parameterised SQL · ownership checks", "secrets masked for nudges + AI", "secrets NOT masked for tracker + "
        "transcript settings"], "app", size=16, align="start")
    arrow(c, [R(files, -40), (490, R(files, -40)[1])], "reads", at=(470, 130))
    arrow(c, [R(env), (680, R(env)[1]), B(flask)], "os.environ", at=(600, R(env)[1]))

    group(c, 920, 10, 670, 300, "Browser", "ui")
    br = box(c, 940, 60, 630, "Session cookie", ["{'email': …} signed with HMAC (itsdangerous) — tamper-proof but "
                                                 "readable, not encrypted.", "Pages escape server text with escapeHtml() (XSS)."],
             "ui", size=16, align="start")
    arrow(c, [L(br), (870, L(br)[1])], color="b", both=True)
    label_at(c, 1255, 260, "Browser ↔ Flask: plain HTTP on localhost\n(no TLS needed while it stays local)", 16, "#2B6CB0")

    group(c, 920, 330, 670, 320, "Public internet (only while ngrok runs)", "risk")
    ng = box(c, 940, 380, 630, "https://<random>.ngrok-free.app → ALL routes", [
        "✔ TLS between caller and ngrok",
        "✖ the whole app is reachable, not only webhooks",
        "✖ the Werkzeug debugger is reachable (debug=True)",
        "✔ Fathom: HMAC-SHA256 checked  ·  ✖ tracker webhook: no secret"], "risk", size=16, align="start")
    arrow(c, [L(ng), (870, L(ng)[1])], "tunnel", at=(895, L(ng)[1] - 24), color="r", both=True)

    group(c, 10, 680, 1580, 270, "External services (all calls leave over TLS)", "ext")
    svcs = [("Google Sheets API", "OAuth bearer token (auto-refreshed)"),
            ("Anthropic / OpenAI-compatible", "API key in a header; transcript text is sent out"),
            ("SMTP · Slack · Teams", "STARTTLS/SSL with certificate checks; HTTPS"),
            ("Fathom · Apps Script", "call us via ngrok")]
    x = 30
    for t, d in svcs:
        b = box(c, x, 740, 370, t, [d], "ext", h=110, size=16)
        x += 390
    arrow(c, [(660, 650), (660, 735)], "outbound: TLS", at=(660, 692), color="o")
    arrow(c, [(1360, 735), (1360, 655)], "inbound webhooks", at=(1360, 692), color="r")
    box(c, 30, 870, 1540, None, ["✔ = control in place   ✖ = known gap (see section 8 and the backlog). "
                                 "Nothing is encrypted at rest: the PC's disk encryption and user account are the protection."],
        "note", size=16)


@diagram("08_webhooks", 1600, 830)
def d_webhooks(c):
    cols = [
        ("POST /api/webhooks/fathom/<token>", [
            ("d", "token known?", "404"), ("d", "provider = fathom?", "400"),
            ("d", "HMAC-SHA256 signature valid? (webhook-id · timestamp · body)", "401"),
            ("b", "_fathom_speaker_turns(payload)", None),
            ("b", "create meeting → extract from turns → COMMIT → auto-sync → 201", None)], "ok"),
        ("POST /api/webhooks/transcript-capture/<token>", [
            ("d", "token known?", "404"), ("d", "title present?", "400"),
            ("d", "transcript is text, not binary?", "400"),
            ("b", "create meeting (source = live) → log inbound", None),
            ("b", "extract → COMMIT → auto-sync → 201", None)], "note"),
        ("POST /api/webhooks/tracker-update", [
            ("d", "email and external_ref present?", "400"),
            ("d", "commitment with that owner email + external_ref?", "404"),
            ("d", "status recognised? (synonyms)", "400"),
            ("b", "UPDATE status · tracker_sync_log inbound", None),
            ("b", "COMMIT → 201", None)], "risk"),
    ]
    for i, (title, steps, kind) in enumerate(cols):
        x = 14 + i * 530
        box(c, x, 10, 500, title, [], kind, h=56, tsize=18)
        y, prev = 90, (x + 200, 66)
        for typ, label, fail in steps:
            if typ == "d":
                b = diamond(c, x + 200, y + 60, 380, 120, label, "note", 16)
                arrow(c, [prev, (x + 200, y)] if prev[0] == x + 200 else [prev, (prev[0], y - 12), (x + 200, y - 12), (x + 200, y)])
                arrow(c, [(x + 390, y + 60), (x + 440, y + 60)], "no", at=(x + 412, y + 46), color="r", lsize=15)
                box(c, x + 440, y + 38, 60, fail, [], "risk", h=44, tsize=17)
                prev, y = (x + 200, y + 120), y + 150
            else:
                b = box(c, x + 20, y, 360, label, [], "app", tsize=16)
                arrow(c, [prev, T(b)], "yes" if prev[1] < y and steps.index((typ, label, fail)) > 0 and
                      steps[steps.index((typ, label, fail)) - 1][0] == "d" else None, at=(x + 222, y - 14), color="g", lsize=15)
                prev, y = B(b), y + b[3] + 28
    notes = [
        ("✔ Strongest: a secret token in the URL and a signature over the body. Gap: webhook-timestamp isn't "
         "checked, so a captured request could be replayed.", "ok"),
        ("Token = secrets.token_urlsafe(24), 192 random bits. It is the only secret — anyone who has the URL can post "
         "meetings. 'Regenerate' rotates it.", "note"),
        ("✖ No secret at all: identified by email + 'row-n'. OK on a laptop, not on a public URL.", "risk"),
    ]
    for i, (n, k) in enumerate(notes):
        box(c, 14 + i * 530, 710, 500, None, [n], k, size=15, h=110)


@diagram("09_ml_training", 1600, 900)
def d_ml(c):
    group(c, 10, 10, 1580, 300, "A. Original training — ml/train_action_detector.py (produced the model in use today)", "ml")
    a = [("Labelled CSVs", ["train_utterances_balanced.csv (44,504 rows)", "test_utterances_balanced.csv"], "db"),
         ("load()", ["x = 'speaker: text'", "y = 1 if label is ACTION"], "mod"),
         ("embed()", ["MiniLM, batch 256", "cached in ml/emb_cache/*.npy"], "ml"),
         ("Split", ["85 % fit / 15 % validation", "stratified, seed 42"], "mod"),
         ("Train MLP", ["256 relu → dropout 0.3 → 128 relu → 1 sigmoid", "adam · binary cross-entropy",
                        "≤ 30 epochs, early stop patience 5"], "ml"),
         ("Evaluate + save", ["report @ 0.5, ROC-AUC", "→ ml/action_detector.keras"], "ok")]
    x, prev = 26, None
    for (t, ls, k), w in zip(a, [250, 200, 230, 200, 300, 240]):
        b = box(c, x, 70, w, t, ls, k, h=200, size=16, tsize=18)
        if prev:
            arrow(c, [R(prev), L(b)])
        prev, x = b, x + w + 18

    group(c, 10, 330, 1580, 560, "B. Retraining on the team's transcripts (2026-10-01) — result kept as a candidate, NOT in use", "note")
    b1 = box(c, 26, 380, 300, "prepare_transcript_dataset.py", [".docx / .txt / .vtt → lines", "grouped into 6 'families'",
                                                                "→ utterances_for_labelling.csv"], "mod", h=170, size=16, tsize=18)
    b2 = box(c, 350, 380, 280, "training_data/labels.py", ["hand-picked ACTION lines", "→ labelled_utterances.csv",
                                                           "2,996 lines · 158 ACTION"], "db", h=170, size=16, tsize=18)
    b3 = box(c, 654, 380, 420, "retrain_with_transcripts.py", [
        "leave-one-family-out: train on old CSV + 5 families (each new line weighted × 8), test on the 6th",
        "threshold picked on a validation slice of the training families"], "ml", h=170, size=16, tsize=18)
    b4 = box(c, 1098, 380, 476, "Result (ml/retrain_report.json)", [
        "macro-F1 0.237 → 0.437 overall",
        "but FY2027 (the one real business meeting) 0.417 → 0.381, recall 0.77 → 0.31"], "risk", h=170, size=16, tsize=18)
    for p, q in ((b1, b2), (b2, b3), (b3, b4)):
        arrow(c, [R(p), L(q)])
    dm = diamond(c, 560, 700, 460, 170, "Better on every family that matters? (decide by hand — see warning)", "note", 17)
    arrow(c, [B(b4), (B(b4)[0], 700), (790, 700)])
    yes = box(c, 26, 625, 250, "yes", ["snapshot first, then copy the candidate over action_detector.keras"],
              "ok", size=16, tsize=18)
    no = box(c, 1000, 760, 574, "no (today's decision)", ["keep action_detector.keras; candidate stays in "
                                                          "ml/action_detector_candidate.keras"], "plain", size=16, tsize=18)
    arrow(c, [(330, 700), R(yes)], color="g")
    arrow(c, [(560, 785), (560, 840), (990, 840), (990, R(no)[1]), L(no)])
    box(c, 26, 800, 500, "⚠ The script overwrites the live model by itself", ["whenever macro-F1 improves. Snapshot "
                                                                              "ml/action_detector.keras before running it."],
        "risk", size=15, tsize=17)


@diagram("10_startup_request", 1600, 900)
def d_startup(c):
    group(c, 10, 10, 610, 880, "Startup — python app.py", "app")
    steps = [
        ("Imports", ["transcript_parser and google_sheets are optional: if their libraries are missing the module "
                     "becomes None and the feature reports 'not installed'"], "mod"),
        ("Module level", ["APP_VERSION = '0.7.3'", "Flask(static_folder='static', static_url_path='')",
                          "app.secret_key (hard-coded)"], "app"),
        ("ai_extraction.init_app()", ["unless AI_EXTRACTION_ENABLED=0 — adds /ai-extraction + /api/ai-extraction/*",
                                      "and creates its 3 tables"], "ml"),
        ("__main__", ["init_db() if the .db file is missing", "migrate_db(): re-run schema.sql + ALTER TABLE for new columns"], "db"),
        ("Reloader child (WERKZEUG_RUN_MAIN=true)", ["start_nudge_scheduler()", "app.run(debug=True, port=5000)"], "app"),
    ]
    y, prev = 60, None
    for t, ls, k in steps:
        b = box(c, 30, y, 570, t, ls, k, size=16, tsize=18)
        if prev:
            arrow(c, [B(prev), T(b)])
        prev, y = b, y + b[3] + 30
    box(c, 30, y + 10, 570, "First extraction is slow (20–60 s)", ["spaCy, MiniLM and Keras load lazily into module "
                                                                   "globals on first use, then stay cached"], "note", size=16, tsize=18)

    group(c, 650, 10, 940, 880, "Every API request", "ui")
    r = [
        ("Page JS → api(path, {method, body})", ["fetch() with JSON; on a non-2xx status throws Error(data.error) → toast()"], "ui"),
        ("Flask route function", ["matched by URL + method (54 route functions)"], "app"),
        ("require_email()", ["reads session['email']; None → 401 'not identified'"], "app"),
        ("Validate input", ["request.get_json(force=True); missing / invalid fields → 400"], "app"),
        ("get_db()", ["sqlite3.connect(timeout=30) · row_factory = Row · PRAGMA foreign_keys = ON"], "db"),
        ("Ownership-scoped SQL", ["always '?' parameters; WHERE … owner_email = ? (get_commitment_or_404 → 404)"], "db"),
        ("commit() · close()", ["one connection per request — nothing is pooled"], "db"),
        ("Side effects after the commit", ["auto_sync_to_sheet / auto_push_status_to_sheet — errors are returned, not raised"], "mod"),
        ("jsonify(result), status code", ["201 created · 200 ok · 4xx with {'error': …}"], "ui"),
    ]
    y, prev = 60, None
    for t, ls, k in r:
        b = box(c, 680, y, 880, t, ls, k, size=16, tsize=18, align="start")
        if prev:
            arrow(c, [B(prev, -300), T(b, -300)])
        prev, y = b, y + b[3] + 18


@diagram("11_debug_triage", 1600, 980)
def d_triage(c):
    heads = [("Symptom", 14, 330, "risk"), ("Look here first", 364, 560, "note"), ("Usual cause → fix", 944, 642, "ok")]
    for t, x, w, k in heads:
        box(c, x, 10, w, t, [], k, h=46, tsize=19)
    rows = [
        ("Page shows old behaviour / port 5000 busy",
         "netstat -ano | findstr :5000 — more than one python PID?",
         "Stale Flask from an earlier run. Stop every python.exe on :5000, start once."),
        ("Upload gives 0 commitments",
         "extraction_error in the upload response; run the parser trace (section 11.3)",
         "Model file / libraries missing, transcript format not recognised (all UNKNOWN), or nothing ≥ 0.90."),
        ("'database is locked'",
         "Is DB Browser / a 2nd app.py / a test holding the file?",
         "Close it. Long ML runs inside the upload transaction — wait or retry."),
        ("Rows not appearing in the Sheet",
         "tracker_sync_log.detail · Settings → auto_sync on?",
         "GoogleAuthNotReadyError / invalid_grant → rerun google_oauth_setup.py (test-mode tokens expire in 7 days)."),
        ("Sheet edit doesn't reach the app",
         "Apps Script → Executions log; HTTP code returned",
         "ngrok URL changed (update the script), simple onEdit instead of an installable trigger, or 404 = wrong email/row."),
        ("Reminder not received",
         "nudge_log.status / detail · channel 'Test' button",
         "Gmail needs an app password; Slack bot needs users:read.email; task has no due date or no owner_email."),
        ("Fathom meeting missing",
         "transcript_sync_log; ngrok inspector http://127.0.0.1:4040",
         "401 = wrong whsec secret; '0 turns' = payload shape changed (_fathom_speaker_turns)."),
        ("AI engine not used",
         "ai_extraction_runs: status fell_back + detail",
         "Consent not given, bad key / base URL, or reply cut off (too long) → built-in model took over."),
    ]
    y = 70
    for s, look, fix in rows:
        h = 100
        box(c, 14, y, 330, None, [s], "risk", h=h, size=17)
        box(c, 364, y, 560, None, [look], "note", h=h, size=16, mono=False)
        box(c, 944, y, 642, None, [fix], "ok", h=h, size=16)
        arrow(c, [(344, y + h / 2), (364, y + h / 2)])
        arrow(c, [(924, y + h / 2), (944, y + h / 2)])
        y += h + 13


# ---------------------------------------------------------------- render

def render(names):
    edge = next((p for p in EDGE_CANDIDATES if p.exists()), None)
    if edge is None:
        sys.exit("Microsoft Edge not found — needed to turn the SVGs into PNGs.")
    OUT.mkdir(exist_ok=True)
    for name in names:
        fn, w, h = DIAGRAMS[name]
        c = Canvas(w, h)
        fn(c)
        svg = c.svg()
        (OUT / f"{name}.svg").write_text(svg, encoding="utf-8")
        png = OUT / f"{name}.png"
        png.unlink(missing_ok=True)
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            page = Path(tmp) / "d.html"
            page.write_text(f"<!doctype html><html><head><meta charset='utf-8'></head><body style='margin:0'>{svg}</body></html>", encoding="utf-8")
            subprocess.run([str(edge), "--headless", "--disable-gpu", "--hide-scrollbars",
                            f"--user-data-dir={Path(tmp) / 'profile'}", "--force-device-scale-factor=1.5",
                            f"--window-size={w},{h}", f"--screenshot={png}", page.as_uri()],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            # msedge.exe can return before the screenshot is on disk
            last, deadline = -1, time.time() + 60
            while time.time() < deadline:
                size = png.stat().st_size if png.exists() else -1
                if size > 0 and size == last:
                    break
                last = size
                time.sleep(0.7)
        print(("ok    " if png.exists() else "FAIL  ") + png.name)


if __name__ == "__main__":
    wanted = sys.argv[1:]
    render([n for n in DIAGRAMS if not wanted or any(w in n for w in wanted)])
