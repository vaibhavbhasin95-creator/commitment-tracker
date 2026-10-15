"""
Generates docs/status-diagram.svg — the "what's live and what's missing" picture
embedded in prd.md. Edit STAGES / FOUNDATIONS below when a status changes, then:

    python src/docs_tools/make_status_diagram.py
"""

from html import escape
from pathlib import Path

AS_OF = "v0.10.0 · 15 Oct 2026"

STATUS = {
    #           fill       stroke     label
    "live":    ("#DCF5E3", "#2E9E5B", "Live — working end-to-end"),
    "built":   ("#DCEBFB", "#2F6FC0", "Built — real integration, first live run pending"),
    "poc":     ("#FFF1D6", "#C98A12", "Proof of concept — simulated or not deployed"),
    "missing": ("#F3F4F6", "#9AA0A6", "Not built yet"),
}

STAGES = [
    ("1  Capture", [
        ("live", "Upload: text, .docx, .pdf", "Live · converted on the server"),
        ("built", "Fathom webhook", "Built · live meeting pending"),
        ("built", "Generic capture webhook", "Built · signed, not used live"),
        ("poc", "Zoom / Meet / Teams / Otter", "Simulated (log only)"),
    ]),
    ("2  Extract", [
        ("live", "Built-in ML model", "Live · weak on real meetings"),
        ("built", "Customer's AI engine", "All paths · Gemini not yet run"),
        ("poc", "Retrained model (0.7.0)", "Candidate · not deployed"),
        ("missing", "Owner / due-date rule fixes", "Not built"),
        ("missing", "Real-meeting training data", "Waiting on transcripts"),
    ]),
    ("3  Assign", [
        ("live", "People directory (CSV)", "Live · 2 people saved"),
        ("live", "Designations", "Live · new in 0.9.0"),
        ("live", "Auto owner tagging", "Live · shared names flagged"),
        ("live", "Manual email override", "Live"),
    ]),
    ("4  Track & sync", [
        ("live", "Dashboard & status", "Live"),
        ("live", "Per-meeting view, clearing", "Live · new in 0.9.0"),
        ("live", "Google Sheets: app → sheet", "Live · every field"),
        ("built", "Google Sheets: sheet → app", "Built · every field, newest wins"),
        ("poc", "Jira / custom tracker", "Simulated (log only)"),
    ]),
    ("5  Nudge", [
        ("built", "Email (Gmail SMTP)", "Built · first real send pending"),
        ("built", "AI-tailored email draft", "Built · by designation"),
        ("built", "Slack (DM or channel)", "Built · not configured"),
        ("built", "Teams (Workflows)", "Built · needs work account"),
        ("built", "Custom webhook", "Built · not configured"),
        ("built", "Automatic schedule", "Built · runs as a job"),
    ]),
]

FOUNDATIONS = [
    ("live", "Password sign-in", "Live · hashed, lockout"),
    ("built", "Single sign-on", "Built · not set up"),
    ("built", "Webhook security", "Built · signed, no replay"),
    ("live", "Secret encryption", "Live · at rest"),
    ("live", "Live updates", "Live · pushed (SSE)"),
    ("live", "Background jobs", "Live · with retries"),
    ("missing", "Hosting", "ngrok today"),
    ("missing", "Team workspaces", "Not built"),
    ("missing", "AI connector (MCP)", "Not built"),
]

W, MARGIN, GAP = 1240, 24, 18
COL_W = (W - 2 * MARGIN - 4 * GAP) / 5
FONT = "Segoe UI, Helvetica Neue, Arial, sans-serif"
INK, MUTED = "#1F2328", "#59636E"


def box(x, y, w, h, status, title, note, title_size=13, note_size=11):
    fill, stroke, _ = STATUS[status]
    dash = ' stroke-dasharray="5 4"' if status == "missing" else ""
    return (
        f'<rect x="{x:.1f}" y="{y}" width="{w:.1f}" height="{h}" rx="8" fill="{fill}" stroke="{stroke}" stroke-width="1.5"{dash}/>'
        f'<rect x="{x:.1f}" y="{y}" width="5" height="{h}" rx="2" fill="{stroke}"/>'
        f'<text x="{x + 14:.1f}" y="{y + 22}" font-size="{title_size}" font-weight="600" fill="{INK}">{escape(title)}</text>'
        f'<text x="{x + 14:.1f}" y="{y + 40}" font-size="{note_size}" fill="{MUTED}">{escape(note)}</text>'
    )


def build():
    out = []
    y_title, y_legend, y_stage = 40, 74, 112
    out.append(f'<text x="{MARGIN}" y="{y_title}" font-size="21" font-weight="700" fill="{INK}">'
               f'Commitment Tracker — what is live and what is missing</text>')
    out.append(f'<text x="{W - MARGIN}" y="{y_title}" font-size="13" fill="{MUTED}" text-anchor="end">{AS_OF}</text>')

    lx = MARGIN
    for key in ("live", "built", "poc", "missing"):
        fill, stroke, label = STATUS[key]
        dash = ' stroke-dasharray="4 3"' if key == "missing" else ""
        out.append(f'<rect x="{lx}" y="{y_legend - 11}" width="16" height="14" rx="3" fill="{fill}" stroke="{stroke}" stroke-width="1.5"{dash}/>')
        out.append(f'<text x="{lx + 22}" y="{y_legend}" font-size="12" fill="{INK}">{escape(label)}</text>')
        lx += 22 + len(label) * 5.7 + 34

    item_h, step = 54, 64
    max_items = max(len(items) for _, items in STAGES)
    for i, (name, items) in enumerate(STAGES):
        x = MARGIN + i * (COL_W + GAP)
        out.append(f'<rect x="{x:.1f}" y="{y_stage}" width="{COL_W:.1f}" height="38" rx="8" fill="#24292F"/>')
        out.append(f'<text x="{x + COL_W / 2:.1f}" y="{y_stage + 25}" font-size="15" font-weight="700" fill="#FFFFFF" text-anchor="middle">{escape(name)}</text>')
        if i < len(STAGES) - 1:
            ax = x + COL_W + 2
            out.append(f'<path d="M{ax:.1f} {y_stage + 19} h{GAP - 8}" stroke="#24292F" stroke-width="2.5" marker-end="url(#arrow)"/>')
        for j, (status, title, note) in enumerate(items):
            out.append(box(x, y_stage + 52 + j * step, COL_W, item_h, status, title, note))

    y_found = y_stage + 52 + max_items * step + 16
    out.append(f'<rect x="{MARGIN}" y="{y_found}" width="{W - 2 * MARGIN}" height="34" rx="8" fill="#57606A"/>')
    out.append(f'<text x="{W / 2}" y="{y_found + 22}" font-size="14" font-weight="700" fill="#FFFFFF" text-anchor="middle">'
               f'Platform foundations (under every stage)</text>')
    n, fgap = len(FOUNDATIONS), 10
    fw = (W - 2 * MARGIN - (n - 1) * fgap) / n
    for k, (status, title, note) in enumerate(FOUNDATIONS):
        out.append(box(MARGIN + k * (fw + fgap), y_found + 46, fw, item_h, status, title, note, 12, 10.5))

    y_note = y_found + 46 + item_h + 30
    out.append(f'<text x="{MARGIN}" y="{y_note}" font-size="12" fill="{MUTED}">'
               f'"Built" items work against test stand-ins; each needs one real run on the test account to turn green. '
               f'Details: "Current status" section of the PRD.</text>')
    height = y_note + 20

    svg = (
        f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {W} {height}" width="{W}" height="{height}" '
        f'font-family="{FONT}" role="img" aria-label="Commitment Tracker status: what is live and what is missing">'
        '<defs><marker id="arrow" viewBox="0 0 10 10" refX="8" refY="5" markerWidth="6" markerHeight="6" orient="auto">'
        '<path d="M0 0 L10 5 L0 10 z" fill="#24292F"/></marker></defs>'
        f'<rect width="{W}" height="{height}" fill="#FFFFFF"/>'
        + "".join(out) + "</svg>"
    )
    path = Path(__file__).resolve().parents[2] / "docs" / "status-diagram.svg"
    path.write_text(svg, encoding="utf-8")
    counts = {k: sum(1 for _, items in STAGES for s, *_ in items if s == k) + sum(1 for s, *_ in FOUNDATIONS if s == k) for k in STATUS}
    print(f"wrote {path} ({W}x{height}) — {counts}")


if __name__ == "__main__":
    build()
