/*
 * Builds docs/Commitment_Tracker_Technical_Guide.docx — the developer handover guide.
 *
 *   python docs/make_guide_diagrams.py              # 1. diagrams → docs/guide_diagrams/*.png
 *   node docs/build_technical_guide.js inventory.json   # 2. the Word file
 *
 * Needs the `docx` npm package (npm install docx). inventory.json is the AST dump of every
 * function/route (name, line, routes, auth) — see the comment at the bottom of this file.
 * Afterwards open the .docx in Word and press F9 on the contents table (or let the build
 * pipeline do it via Word COM) so the page numbers fill in.
 */
const fs = require("fs");
const path = require("path");
const {
  Document, Packer, Paragraph, TextRun, HeadingLevel, AlignmentType, Table, TableRow, TableCell,
  WidthType, ShadingType, BorderStyle, ImageRun, PageOrientation, TableOfContents, Header, Footer,
  PageNumber, LevelFormat, InternalHyperlink, Bookmark, PageBreak, TableLayoutType, VerticalAlign,
} = require("docx");

const DOCS = __dirname;
const INVENTORY = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
const OUT = path.join(DOCS, "Commitment_Tracker_Technical_Guide.docx");
const VERSION = "0.7.3";

// ------------------------------------------------------------------ styling helpers
const C = { ink: "1A202C", muted: "4A5568", blue: "2B6CB0", green: "2F855A", red: "C53030", amber: "B7791F",
  purple: "6B46C1", headBg: "1F3A5F", zebra: "F4F7FA", codeBg: "F3F4F6", line: "CBD5E0" };
const PORTRAIT_W = 9906; // text width in DXA (A4 11906 − 2 × 1000 margins)
const FONT = "Calibri";
const MONO = "Consolas";

// Inline markup: **bold**, `code`, [[text|anchor]] internal link.
function runs(text, base = {}) {
  const out = [];
  const re = /(\*\*[^*]+\*\*|`[^`]+`|\[\[[^\]]+\]\])/g;
  let last = 0, m;
  while ((m = re.exec(text))) {
    if (m.index > last) out.push(new TextRun({ text: text.slice(last, m.index), ...base }));
    const tok = m[0];
    if (tok.startsWith("**")) out.push(new TextRun({ text: tok.slice(2, -2), bold: true, ...base }));
    else if (tok.startsWith("`")) out.push(new TextRun({ text: tok.slice(1, -1), font: MONO, size: (base.size || 21) - 2, color: "9B2C2C", ...base.codeOverride }));
    else {
      const [label, anchor] = tok.slice(2, -2).split("|");
      out.push(new InternalHyperlink({ anchor, children: [new TextRun({ text: label, style: "Hyperlink", ...base })] }));
    }
    last = m.index + tok.length;
  }
  if (last < text.length) out.push(new TextRun({ text: text.slice(last), ...base }));
  return out;
}
const p = (text, opts = {}) => new Paragraph({ children: runs(text, opts.run || {}), spacing: { after: 120, line: 276 }, ...opts.para });
const small = (text) => p(text, { run: { size: 18, color: C.muted } });
let bm = 0;
function heading(level, text, anchor) {
  const children = anchor ? [new Bookmark({ id: anchor, children: [new TextRun(text)] })] : [new TextRun(text)];
  return new Paragraph({ heading: level, children, ...(level === HeadingLevel.HEADING_1 ? { pageBreakBefore: true } : {}) });
}
const h1 = (t, a) => heading(HeadingLevel.HEADING_1, t, a);
const h2 = (t, a) => heading(HeadingLevel.HEADING_2, t, a);
const h3 = (t, a) => heading(HeadingLevel.HEADING_3, t, a);

let listInstance = 0;
const bullets = (items, level = 0) => items.map((t) => Array.isArray(t)
  ? bullets(t, level + 1) : new Paragraph({ numbering: { reference: "bullets", level }, children: runs(t), spacing: { after: 60, line: 264 } })).flat();
function steps(items) {
  const inst = ++listInstance;
  return items.map((t) => new Paragraph({ numbering: { reference: "steps", level: 0, instance: inst }, children: runs(t), spacing: { after: 70, line: 264 } }));
}
function code(lines, caption) {
  const out = [];
  if (caption) out.push(new Paragraph({ children: [new TextRun({ text: caption, italics: true, size: 18, color: C.muted })], spacing: { before: 60, after: 40 } }));
  const ls = Array.isArray(lines) ? lines : lines.split("\n");
  ls.forEach((l, i) => out.push(new Paragraph({
    children: [new TextRun({ text: l === "" ? " " : l, font: MONO, size: 17, color: C.ink })],
    shading: { type: ShadingType.CLEAR, fill: C.codeBg },
    border: { left: { style: BorderStyle.SINGLE, size: 18, color: "A0AEC0", space: 8 } },
    spacing: { after: i === ls.length - 1 ? 140 : 0, line: 240 }, indent: { left: 140 },
  })));
  return out;
}
const BORDER = { style: BorderStyle.SINGLE, size: 4, color: C.line };
const BORDERS = { top: BORDER, bottom: BORDER, left: BORDER, right: BORDER };
function cell(content, width, { head = false, fill, bold = false, size = 18, mono = false, color } = {}) {
  const paras = (Array.isArray(content) ? content : [content]).map((t) => new Paragraph({
    children: head ? [new TextRun({ text: t, bold: true, color: "FFFFFF", size })]
      : runs(String(t), { size, ...(bold ? { bold: true } : {}), ...(mono ? { font: MONO } : {}), ...(color ? { color } : {}) }),
    spacing: { after: 30, line: 252 },
  }));
  return new TableCell({ children: paras, width: { size: width, type: WidthType.DXA }, borders: BORDERS,
    shading: { type: ShadingType.CLEAR, fill: head ? C.headBg : (fill || "FFFFFF") },
    margins: { top: 50, bottom: 50, left: 90, right: 90 }, verticalAlign: VerticalAlign.TOP });
}
function table(headers, rows, ratios, opts = {}) {
  const total = opts.width || PORTRAIT_W;
  const sum = ratios.reduce((a, b) => a + b, 0);
  const widths = ratios.map((r) => Math.floor((r / sum) * total));
  widths[widths.length - 1] += total - widths.reduce((a, b) => a + b, 0);
  const size = opts.size || 18;
  const trs = [new TableRow({ tableHeader: true, children: headers.map((h, i) => cell(h, widths[i], { head: true, size })) })];
  rows.forEach((r, ri) => {
    const fill = (!Array.isArray(r) && r.rowFill) || (ri % 2 ? C.zebra : "FFFFFF");
    const cells = r.cells || r;
    trs.push(new TableRow({ cantSplit: true, children: cells.map((c, i) => cell(c, widths[i], {
      fill, size, bold: opts.boldFirst && i === 0, mono: opts.monoCols && opts.monoCols.includes(i) })) }));
  });
  return [new Table({ width: { size: total, type: WidthType.DXA }, columnWidths: widths, rows: trs, layout: TableLayoutType.FIXED }),
    new Paragraph({ children: [], spacing: { after: 100 } })];
}
const KIND = { info: ["EBF4FF", C.blue, "ℹ "], tip: ["EAF7EE", C.green, "✔ "], warn: ["FFF8E6", C.amber, "⚠ "], risk: ["FDECEC", C.red, "✖ "] };
function callout(kind, title, body) {
  const [fill, color, icon] = KIND[kind];
  const paras = [new Paragraph({ children: [new TextRun({ text: icon + title, bold: true, color, size: 20 })], spacing: { after: 40 } })];
  (Array.isArray(body) ? body : [body]).forEach((t) => paras.push(new Paragraph({ children: runs(t, { size: 19 }), spacing: { after: 40, line: 260 } })));
  const side = { style: BorderStyle.SINGLE, size: 4, color: fill };
  return [new Table({ width: { size: PORTRAIT_W, type: WidthType.DXA }, columnWidths: [PORTRAIT_W], layout: TableLayoutType.FIXED,
    rows: [new TableRow({ cantSplit: true, children: [new TableCell({ children: paras, width: { size: PORTRAIT_W, type: WidthType.DXA },
      shading: { type: ShadingType.CLEAR, fill },
      borders: { top: side, bottom: side, right: side, left: { style: BorderStyle.SINGLE, size: 36, color } },
      margins: { top: 90, bottom: 90, left: 160, right: 140 } })] })] }),
  new Paragraph({ children: [], spacing: { after: 100 } })];
}

// ------------------------------------------------------------------ sections & figures
const header = () => ({ default: new Header({ children: [new Paragraph({ alignment: AlignmentType.RIGHT,
  children: [new TextRun({ text: `Commitment Tracker — Technical Handover Guide · v${VERSION}`, size: 16, color: "718096" })] })] }) });
const footer = () => ({ default: new Footer({ children: [new Paragraph({ alignment: AlignmentType.CENTER, children: [
  new TextRun({ children: ["Page ", PageNumber.CURRENT, " of ", PageNumber.TOTAL_PAGES], size: 16, color: "718096" })] })] }) });
const PORTRAIT = { page: { size: { width: 11906, height: 16838 }, margin: { top: 1000, bottom: 900, left: 1000, right: 1000, header: 500, footer: 450 } } };
const LANDSCAPE = { page: { size: { width: 11906, height: 16838, orientation: PageOrientation.LANDSCAPE },
  margin: { top: 640, bottom: 600, left: 720, right: 720, header: 360, footer: 330 } } };

const sections = [];
let current = [];
function flush() {
  if (current.length) sections.push({ properties: PORTRAIT, headers: header(), footers: footer(), children: current });
  current = [];
}
const add = (...items) => items.flat().forEach((i) => current.push(i));

function pngSize(file) {
  const b = fs.readFileSync(file);
  return { w: b.readUInt32BE(16), h: b.readUInt32BE(20) };
}
let figNo = 0;
const figures = [];
function figure(file, title, howTo, anchor) {
  figNo += 1;
  if (FIG_ORDER[figNo - 1] !== anchor) throw new Error(`figure order mismatch: ${anchor} is #${figNo}`);
  figures.push([figNo, title, anchor]);
  const full = path.join(DOCS, "guide_diagrams", file);
  const { w, h } = pngSize(full);
  const maxW = 10.55 * 96, maxH = 5.95 * 96;
  const scale = Math.min(maxW / w, maxH / h);
  flush();
  sections.push({ properties: LANDSCAPE, headers: header(), footers: footer(), children: [
    new Paragraph({ children: [new Bookmark({ id: anchor, children: [new TextRun({ text: `Figure ${figNo} — ${title}`, bold: true, size: 26, color: C.headBg })] })],
      spacing: { after: 60 } }),
    new Paragraph({ children: runs("**How to read it:** " + howTo, { size: 18, color: C.muted }), spacing: { after: 100 } }),
    new Paragraph({ alignment: AlignmentType.CENTER, children: [new ImageRun({ type: "png", data: fs.readFileSync(full),
      transformation: { width: Math.round(w * scale), height: Math.round(h * scale) },
      altText: { title: title, description: howTo, name: file } })] }),
  ] });
}
// Figures are numbered in page order; text refers to them by anchor so numbers can't drift.
const FIG_ORDER = ["f_arch", "f_start", "f_seq", "f_sheets", "f_nudge", "f_hooks", "f_pipe", "f_er", "f_trust", "f_ml", "f_triage"];
const seeFig = (_n, anchor, label) => `[[Figure ${FIG_ORDER.indexOf(anchor) + 1}${label ? " — " + label : ""}|${anchor}]]`;

// ================================================================== CONTENT
// ---------- cover
add(
  new Paragraph({ children: [], spacing: { before: 2200 } }),
  new Paragraph({ children: [new TextRun({ text: "Commitment Tracker", bold: true, size: 64, color: C.headBg })], spacing: { after: 120 } }),
  new Paragraph({ children: [new TextRun({ text: "Technical Handover Guide", size: 40, color: C.blue })], spacing: { after: 400 } }),
  new Paragraph({ children: [new TextRun({ text: "How the code works, end to end — flows, every function, data, security, ML, testing and debugging",
    size: 24, color: C.muted, italics: true })], spacing: { after: 700 } }),
  ...table(["Item", "Value"], [
    ["Application version", `${VERSION} (see APP_VERSION in app.py and CHANGELOG.md)`],
    ["Date", "2 October 2026"],
    ["Written for", "A senior engineer (10+ years) who is newer to Python and to AI/ML, taking over development"],
    ["Repository", "C:\\PersonalDevelopment\\commitment-tracker (local; last pushed commit 0b5e703 — most work since 0.2.0 is not committed yet)"],
    ["Companion documents", "prd.md / Commitment_Tracker_PRD_v0.7.2.pdf (what and why) · readme.md (how to run) · CHANGELOG.md (history)"],
    ["Contains secrets?", "No. Every credential is described by location only — never by value."],
  ], [28, 72], { boldFirst: true }),
);
add(new Paragraph({ children: [new PageBreak()] }),
  new Paragraph({ children: [new TextRun({ text: "Contents", bold: true, size: 32, color: C.headBg })], spacing: { after: 120 } }),
  new TableOfContents("Contents", { hyperlink: true, headingStyleRange: "1-2" }),
  small("If page numbers show as missing, right-click the table and choose Update Field → Update entire table."),
);

// ---------- how to use
add(h1("How to use this guide", "howto"),
  p("This is the document I would want on my first day. It is written to be read in order once, then used as a reference. " +
    "Every function name, table, setting and number in it was taken from the code at version " + VERSION + " — where the code and " +
    "this guide ever disagree, the code wins and this guide should be updated."),
  ...table(["If you have…", "Read", "You will be able to…"], [
    ["2 hours (day 1)", "[[1 System at a glance|s1]] · [[2 Code flow|s2]] · [[11 Diagnose & debug|s11]]", "run the app, follow a transcript through the code, fix the common breakages"],
    ["Before changing extraction", "[[5 Chunking|s5]] · [[10 ML primer|s10]] · [[3.2 transcript_parser.py|r_tp]]", "change parsing or the model without silently making results worse"],
    ["Before touching data or schema", "[[6 Databases|s6]] · [[12 Working conventions|s12]]", "add a column or table with a safe migration"],
    ["Before exposing it beyond a laptop", "[[4 Encryption|s4]] · [[7 Credentials|s7]] · [[8 Security|s8]] · [[9 Edge cases & testing|s9]]", "know exactly what is protected, what isn't, and how to prove a change didn't open a hole"],
    ["A specific question", "[[3 Function reference|s3]] · [[Appendix A routes|appA]] · [[Appendix C glossary|appC]]", "look up any function, route or term"],
  ], [22, 42, 36]),
  p("**Conventions.** `monospace` is code, a file or a command. Line numbers like `app.py:567` refer to version " + VERSION +
    " and will drift as the code changes — search for the function name if they no longer match. Boxes marked ⚠ or ✖ are " +
    "known weak spots; ✔ boxes are things done deliberately that should be kept."),
  ...callout("info", "Diagrams", "The 11 diagrams are on landscape pages so they stay readable. They are generated by " +
    "`python docs/make_guide_diagrams.py` (SVG + PNG in `docs/guide_diagrams/`), so edit that script rather than the pictures."),
);

// ---------- 1. system at a glance
add(h1("1  System at a glance", "s1"),
  h2("1.1  What the application does"),
  p("Commitment Tracker turns meeting transcripts into a tracked list of who promised to do what, by when — then keeps that list " +
    "in sync with a Google Sheet and reminds people before things slip. Five jobs, in the order data flows:"),
  ...steps([
    "**Capture** a transcript — manual upload of a .txt file, a generic JSON webhook, or Fathom's webhook (real, via an ngrok tunnel).",
    "**Extract** commitments — a small trained neural network (built-in) or, per account, an LLM (Claude or any OpenAI-compatible API such as Gemini).",
    "**Tag owners** — turn a spoken name (\"Bhuvan\") into an email using the account's People directory.",
    "**Track & sync** — status pending / in progress / done in the app, mirrored both ways with a Google Sheet.",
    "**Nudge** — email, Slack, Teams or a custom webhook reminder the day before, on the day, or daily once overdue.",
  ]),
  h2("1.2  Technology stack"),
  ...table(["Layer", "Technology", "Notes for a newcomer"], [
    ["Web server", "Python 3 · Flask (dev server, debug=True)", "One process, one file of routes (`app.py`). No ORM, no blueprints except the optional AI feature."],
    ["Frontend", "Static HTML pages + vanilla JavaScript (`static/`)", "No build step, no framework. Each page calls JSON APIs through `api()` in `app.js`."],
    ["Database", "SQLite file `commitment_tracker.db` via the `sqlite3` standard library", "Plain SQL with `?` parameters. Schema in `schema.sql`, upgrades in `migrate_db()`."],
    ["NLP / ML", "spaCy `en_core_web_sm` · sentence-transformers `all-MiniLM-L6-v2` · TensorFlow/Keras · pandas", "Loaded lazily on the first extraction (20–60 s once), then cached in memory."],
    ["LLM (optional)", "Anthropic Python SDK · or raw HTTPS to any `/chat/completions` endpoint", "Per-account choice with explicit consent; failures fall back to the built-in model."],
    ["Google", "google-api-python-client + google-auth-oauthlib (OAuth Desktop flow)", "Token cached in `google_token.json`; Sheets API v4 values.get / values.update."],
    ["Messaging", "smtplib + urllib (standard library only)", "Email, Slack, Teams Workflows, custom JSON webhook."],
    ["Tunnel", "ngrok (free plan)", "Gives the laptop a public https URL so Fathom / Apps Script can reach it."],
    ["Tests", "Plain Python scripts in `tests/` (no pytest needed)", "`python tests/run_all.py` — 119 checks, temporary DBs, fake external services."],
  ], [16, 36, 48]),
  h2("1.3  Repository map"),
  ...table(["Path", "What lives there", "How often you'll touch it"], [
    ["`app.py` (2,152 lines)", "All Flask routes and the glue: DB helpers, upload, extraction hand-off, sync, nudges, webhooks", "Constantly"],
    ["`transcript_parser.py`", "Transcript → turns → sentences → ML classifier → owner/task/due rules", "When extraction quality matters"],
    ["`ai_extraction.py`", "Optional LLM engine, its 3 tables and its routes (marked `[ai-extraction v0.5.0]`)", "Rarely; it is self-contained"],
    ["`people_directory.py`", "CSV parsing and name → email matching", "Rarely"],
    ["`notifications.py`", "One sender per nudge channel; never raises", "When adding a channel"],
    ["`google_sheets.py` · `google_oauth_setup.py`", "Sheets API calls · one-time OAuth consent script", "Rarely"],
    ["`schema.sql`", "CREATE TABLE IF NOT EXISTS for the 11 core tables", "With every new column (plus `migrate_db`)"],
    ["`static/`", "5 pages (`index`, `dashboard`, `people`, `settings`, `ai-extraction`) + `app.js` + `style.css`", "For any UI change"],
    ["`ml/`", "Training/retraining scripts, the live model `action_detector.keras`, a not-deployed candidate, embedding cache", "Only when retraining"],
    ["`training_data/`", "Labelled lines from the team's transcripts (`labelled_utterances.csv`, `labels.py`)", "When labelling more data"],
    ["`tests/`", "4 suites + `run_all.py` + `_support.py` (temp app, fake Google Sheet)", "With every change"],
    ["`docs/`", "PRD PDF, status diagram, this guide and the scripts that build them", "When documenting"],
    ["`_snapshots/`", "Copies of files before risky changes, plus a revert script (gitignored)", "Before risky work"],
    ["`CHANGELOG.md`", "Every change, by version — the project's history while git is not being used", "Every change"],
  ], [26, 56, 18]),
  h2("1.4  What is real and what is still a mock"),
  ...table(["Area", "Real (talks to the outside world)", "Mock / not built"], [
    ["Transcript in", "Upload · generic capture webhook · Fathom webhook (HMAC-verified)", "\"Check now\" pull for Zoom / Meet / Teams / Otter only writes a log line"],
    ["Extraction", "Built-in model · Claude · OpenAI-compatible (Gemini tested)", "Demo meetings use fixed sample data, not the model"],
    ["Tracker", "Google Sheets both directions (API out, Apps Script webhook in)", "Jira and Custom tracker: log only"],
    ["Reminders", "Email (SMTP) · Slack · Teams · custom webhook · 15-minute scheduler", "—"],
    ["People", "CSV upload, auto-tagging, manual override", "No sync with a corporate directory (AD/Okta)"],
    ["Sign-in", "—", "No password / SSO: an email address is the whole identity"],
  ], [16, 46, 38]),
  ...callout("warn", "Stale comment to know about", "The big docstring at the top of `app.py` still says nudges and Google Sheets are mocked. " +
    "That was true at 0.1.0; both are real now. Trust the code and CHANGELOG.md, and fix that docstring when you next edit the file."),
  p(`The picture of how these pieces connect is ${seeFig(1, "f_arch", "system architecture")}.`),
);
figure("01_architecture.png", "System architecture", "left to right is browser → Flask routes → helper modules → outside world. " +
  "Solid arrows are calls the app makes; red dashed arrows are inbound webhooks that arrive through the ngrok tunnel. " +
  "Grey boxes are files on disk.", "f_arch");

add(h2("1.5  Python idioms you'll meet in this codebase"),
  p("You know how to program; these are the Python-specific patterns that make this code read differently from Java/C#/JS."),
  ...table(["Idiom", "Example here", "What it means"], [
    ["Decorator", "`@app.route(\"/api/meetings\", methods=[\"POST\"])`", "Registers the function below it as the handler for that URL + method. No routing table elsewhere."],
    ["Optional import", "`try: import transcript_parser` / `except ImportError: transcript_parser = None`", "The app starts even if heavy ML libraries are missing; callers check for None."],
    ["Lazy singleton", "`global _model` … `if _model is None: _model = load()`", "Module-level cache: the first call pays the load cost, later calls reuse it. Lives until the process restarts."],
    ["Tuple return for errors", "`count, error = auto_extract_commitments(...)`", "Instead of throwing, many helpers return (value, error_or_None) so a failure can be shown without aborting the request."],
    ["Truthy defaults", "`(data.get(\"title\") or \"\").strip()`", "`dict.get` returns None if missing; `or \"\"` turns None/empty into a safe string."],
    ["Context manager", "`with sqlite3.connect(DB_PATH) as conn:`", "Commits (or rolls back) automatically at the end of the block. Note: it does not close the connection."],
    ["Comprehension", "`{row[1] for row in conn.execute(\"PRAGMA table_info(x)\")}`", "Builds a set/list/dict in one expression — like a map/filter."],
    ["f-string", "`f\"row-{first_row + i}\"`", "String interpolation. Never used to build SQL values (that's what `?` is for)."],
    ["`__main__` guard", "`if __name__ == \"__main__\":`", "Code that runs only when the file is executed directly, not when imported by tests."],
    ["pandas DataFrame", "`df[df.text.str.len() > 0]`", "A table in memory; boolean masks filter rows. Used only in the parser and ML scripts."],
    ["Compiled regex", "`HEADER = re.compile(r\"...\")`", "`r\"\"` is a raw string (backslashes kept). `.match` anchors at the start, `.search` scans."],
  ], [16, 40, 44]),
);

// ---------- 2. code flow
add(h1("2  Code flow", "s2"),
  p("This section follows the code the way the data moves. Read it with the diagrams open; the function reference in section 3 has the detail."),
  h2("2.1  Startup and the life of a request", "s2_1"),
  p(`${seeFig(10, "f_start", "startup and request lifecycle")} shows both. In words:`),
  ...steps([
    "`python app.py` imports the helper modules. `transcript_parser` and `google_sheets` are optional imports — if their libraries are missing the variable is None and the feature reports \"not installed\" instead of crashing.",
    "At module level: `APP_VERSION`, the Flask object (serving `static/` at the site root) and `app.secret_key` are set, then `ai_extraction.init_app()` registers the optional AI routes unless `AI_EXTRACTION_ENABLED=0`.",
    "In `__main__`: `init_db()` creates the database from `schema.sql` if the file doesn't exist, then `migrate_db()` always runs (it is safe to repeat) to add any newer columns.",
    "Flask's debug reloader starts a second copy of the process. Only that child (env `WERKZEUG_RUN_MAIN=true`) starts the nudge scheduler thread, so reminders aren't sent twice.",
    "Each browser action calls `api(path, {method, body})` in `app.js` → the matching route → `require_email()` reads the signed session cookie (401 if absent) → input validation (400) → `get_db()` → parameterised SQL scoped to that email → commit/close → side effects such as Sheet sync → `jsonify(...)`.",
  ]),
  ...callout("tip", "Pattern worth keeping", "Side effects that call the internet (Sheets, LLM, email) happen after the main commit, and return " +
    "an error string rather than raising. A Google outage can therefore never lose an upload — the user sees `tracker_sync_error` instead."),
);
figure("10_startup_request.png", "Startup and the life of every API request", "left column runs once when you start the app; " +
  "right column runs for every click in the UI. Grey boxes touch the database.", "f_start");

add(h2("2.2  From uploaded transcript to commitments", "s2_2"),
  p(`This is the core flow. ${seeFig(2, "f_seq", "upload sequence")} draws it as a sequence diagram; the numbers below match the arrows.`),
  ...table(["#", "What happens", "Where"], [
    ["1", "Dashboard posts `{title, meeting_date, transcript_excerpt}`. The browser has already refused non-.txt files.", "`dashboard.html` → `create_meeting` `app.py:567`"],
    ["2–3", "No session → 401. Text that looks like a ZIP/.docx or is >1 % replacement characters → 400.", "`require_email`, `looks_like_binary_text` `app.py:550`"],
    ["4", "The meeting row is inserted. This opens SQLite's write transaction, which stays open until step 14.", "`create_meeting_row` `app.py:413`"],
    ["5–7", "If the account chose an AI engine and gave consent, the transaction is committed early, the LLM is called and its items returned. Any failure or the built-in choice returns None.", "`ai_extraction.try_extract` `ai_extraction.py:313`"],
    ["8–10", "Built-in path: the transcript runs through the parser and classifier (section 5) and comes back as a list of dicts.", "`transcript_parser.extract_commitments`"],
    ["11–12", "Each spoken owner name is matched against the People directory → email + match status.", "`people_directory.match_owner`"],
    ["13", "Commitments are inserted with status `pending`. A sentence already saved for this meeting is skipped, so re-running extraction never duplicates.", "`_insert_extracted_commitments` `app.py:477`"],
    ["14", "Commit and close. From here the upload is permanent whatever happens next.", "`create_meeting`"],
    ["15–17", "If the Sheets tracker has auto-sync on: take the write lock, append the new rows, store `external_ref = row-n`.", "`auto_sync_to_sheet` `app.py:1454`"],
    ["18", "Response `201` with counts and any `extraction_error` / `tracker_sync_error`; the page shows a toast and reloads the list.", "`create_meeting`"],
  ], [6, 62, 32]),
  ...callout("warn", "Performance trap", "With the built-in engine, steps 8–13 run while the write transaction from step 4 is still open. A long transcript (or the 20–60 s model load on the first extraction after a restart) " +
    "holds SQLite's write lock that long, and another write in that window waits up to 30 s and then fails with \"database is locked\". The AI path avoids this by committing first. " +
    "A good next refactor: commit the meeting row before calling the built-in parser too."),
);
figure("02_upload_sequence.png", "Upload → commitments, as a sequence", "time runs downwards; each vertical dashed line is a module. " +
  "Purple messages are extraction, orange is Google, dashed arrows are return values. The dotted box is the optional AI path.", "f_seq");

add(h2("2.3  All the ways a meeting can arrive"),
  ...table(["Entry point", "Route → function", "Runs extraction?", "Auth"], [
    ["Manual upload", "`POST /api/meetings` → `create_meeting`", "Yes (text pipeline)", "session"],
    ["Generic capture webhook", "`POST /api/webhooks/transcript-capture/<token>` → `transcript_capture_webhook`", "Yes (text pipeline)", "URL token"],
    ["Fathom webhook", "`POST /api/webhooks/fathom/<token>` → `fathom_webhook`", "Yes (turns pipeline — skips `to_turns`)", "URL token + HMAC"],
    ["\"Simulate incoming capture\" button", "`POST /api/meetings/simulate-incoming-capture`", "Yes (same code as a webhook)", "session"],
    ["Re-extract an existing meeting", "`POST /api/meetings/<id>/extract-commitments`", "Yes, adds only new sentences", "session"],
    ["Demo data", "`POST /api/meetings/seed-demo` → `seed_demo`", "No — fixed `DEMO_MEETINGS` rows", "session"],
    ["Add one task by hand", "`POST /api/commitments` → `create_commitment`", "No (confidence NULL)", "session"],
  ], [22, 46, 20, 12]),
  h2("2.4  Status changes and the two-way Google Sheet sync", "s2_4"),
  p(`${seeFig(6, "f_sheets", "two-way Sheets sync")} shows both directions. Key design points:`),
  ...bullets([
    "**Outbound** writes use an explicit range `A{n}:E{m}` after reading column A to find the next empty row. `values.append` was tried first and shifted rows right whenever a due date cell was empty.",
    "**Locking:** `auto_sync_to_sheet` and `sync_all_to_tracker` start with `BEGIN IMMEDIATE`, so two near-simultaneous requests can't both send the same unsynced rows (this bug happened; test \"two simultaneous syncs write exactly one row\" guards it).",
    "**The link** between a task and its sheet row is `commitments.external_ref = 'row-<n>'`. Inserting or sorting rows by hand in the sheet breaks that link — tell users not to sort the sheet.",
    "**Inbound:** an installable Apps Script trigger `onStatusEdit` posts `{email, external_ref, status}` to `/api/webhooks/tracker-update` through ngrok; `normalize_tracker_status` accepts synonyms such as Closed, WIP, To do.",
    "**No echo loop:** edits made through the API don't fire the sheet's On-edit trigger, so an app → sheet update never bounces back.",
  ]),
);
figure("06_sheets_two_way.png", "Two-way Google Sheets sync", "top half is app → sheet, bottom half is sheet → app. " +
  "Red boxes are where things are exposed or most often break (ngrok URL, missing webhook secret).", "f_sheets");

add(h2("2.5  Reminders (nudges)", "s2_5"),
  p(`${seeFig(5, "f_nudge", "nudge scheduler")}. A daemon thread wakes every \`NUDGE_CHECK_INTERVAL_SECONDS\` (default 900). For each account that ` +
    "switched automatic nudges on, `due_nudges` pairs every enabled channel with every open task that has a due date and keeps the ones whose cadence matches today:"),
  ...table(["Cadence (UI label)", "Fires when", "Repeats?"], [
    ["`day_before` (1 day before due)", "due_date = tomorrow", "Once; also not retried within 20 h"],
    ["`on_due` (On the due date)", "due_date = today", "Once; also not retried within 20 h"],
    ["`daily_after_overdue` (reason `overdue`)", "due_date < today", "At most once per 20 h per task and channel"],
  ], [34, 30, 36]),
  p("`send_and_log` then calls `notifications.deliver`, which never raises and always returns `(status, detail)`; the result goes into `nudge_log`. " +
    "Tasks without a due date, done tasks, and (for email) tasks without an `owner_email` are never sent — the last one is logged as `skipped` with a reason."),
);
figure("05_nudge_scheduler.png", "Reminder scheduler", "start top-left; the two diamonds decide whether a (channel, task) pair is sent. " +
  "Everything ends in a nudge_log row, which is the first place to look when a reminder 'didn't arrive'.", "f_nudge");

add(h2("2.6  Frontend pages"),
  ...table(["Page", "Purpose", "Main API calls"], [
    ["`index.html`", "Enter an email to start a session", "`/api/identify`, `/api/me`"],
    ["`dashboard.html`", "Meetings list, transcript upload, task list, status/owner edits, per-task history and sync", "`/api/meetings`, `/api/commitments`, `/api/commitments/<id>`, `/sync-tracker`, `/send-test-nudge`"],
    ["`people.html`", "Upload the directory CSV, edit people, see tagging counts", "`/api/people*`"],
    ["`settings.html`", "Nudge channels, tracker, transcript source, webhook URLs, Apps Script snippet", "`/api/nudge-settings`, `/api/tracker-settings`, `/api/transcript-source-settings`, `/api/setup-steps/suggest`"],
    ["`ai-extraction.html`", "Pick an engine, consent, test, compare engines on a meeting, score them", "`/api/ai-extraction/*`"],
    ["`app.js`", "Shared helpers: `api()`, `toast()`, `escapeHtml()`, date formatting, `requireIdentified()`, nav + theme", "—"],
  ], [18, 44, 38]),
  ...callout("risk", "Rule for any UI change", "Any text that came from the server or a user (task text, names, error detail, a provider's label) must go through " +
    "`escapeHtml()` before it is put into `innerHTML`. Version 0.7.3 fixed five places that missed this. `toast()` is safe because it uses `textContent`."),
  h2("2.7  Error-handling philosophy"),
  ...bullets([
    "**4xx are expected:** every route returns JSON `{\"error\": \"…\"}` with 400 (bad input), 401 (no session), 404 (not yours / not found). The frontend shows `data.error` in a toast.",
    "**Never-raise wrappers** around anything external: `notifications.deliver`, `ai_extraction.try_extract`, `auto_sync_to_sheet`, `auto_push_status_to_sheet`, `_fathom_speaker_turns`. They return an error string or None.",
    "**Keep the primary write:** a meeting is kept even if extraction or sync fails; the failure is reported in the response and in a log table.",
    "**Background thread** catches everything per pass and prints `[nudges] scheduler pass failed: …` so one bad row can't stop future reminders.",
  ]),
);

// ---------- 3. function reference
const DESC = {
  // app.py
  get_db: "Opens a new SQLite connection: 30 s lock timeout, rows readable by column name, foreign keys enforced. Callers must close it.",
  init_db: "Creates a brand-new database by running `schema.sql`. Only called when the .db file doesn't exist.",
  migrate_db: "Brings an older database up to date: re-runs `schema.sql` (all CREATE IF NOT EXISTS) then adds missing columns with ALTER TABLE after checking `PRAGMA table_info`. Safe to run any number of times.",
  now_iso: "Current UTC time as `YYYY-MM-DDTHH:MM:SS` — the format of every *_at column.",
  today_str: "Today's local date as `YYYY-MM-DD` — default meeting date.",
  current_email: "The email stored in the signed session cookie, or None.",
  require_email: "Same as `current_email`; every protected route calls it first and returns 401 when it is None.",
  index: "Serves the sign-in page.", dashboard_page: "Serves the dashboard page.", settings_page: "Serves the settings page.", people_page: "Serves the People directory page.",
  _person_fields: "Cleans and validates one person from a request body (email required, plus at least one name or user name).",
  _after_directory_change: "Re-runs owner matching for the account's tasks and commits; returns counts per match status.",
  list_people: "Lists the account's directory plus counts of tasks by owner-match status.",
  people_template: "Downloads the sample CSV header (First Name, Last Name, Email ID, Corporate User Name).",
  upload_people: "Imports a directory CSV in merge or replace mode, reports row errors, then re-matches task owners.",
  add_person: "Adds one person (400 on duplicate email) and re-matches owners.",
  change_person: "Edits (PATCH) or deletes (DELETE) one person — scoped to the signed-in account — and re-matches owners.",
  version: "Returns `APP_VERSION` (shown in the nav bar).",
  identify: "\"Sign-in\": validates the email has an @, upserts it into `users`, stores it in the session. No password.",
  me: "Returns the session email or 401 — pages use it to check they are signed in.",
  logout: "Removes the email from the session.",
  list_meetings: "The account's meetings with commitment counts, newest first.",
  create_meeting_row: "Single INSERT point for a meeting (upload, webhooks, simulate). Returns the new id; does not commit.",
  auto_extract_commitments: "Hands a transcript to the AI engine (if chosen) or the built-in parser and inserts the results. Returns (count, error); never raises.",
  auto_extract_commitments_from_turns: "Same as above for sources that already give speaker turns (Fathom), skipping `to_turns`.",
  _insert_extracted_commitments: "Inserts extracted items as pending commitments, skipping quotes already saved for the meeting, and tags owners via `match_owner`.",
  load_people: "The account's People directory as a list of dicts.",
  load_people_for_meeting: "The directory of whichever account owns a meeting.",
  rematch_owners: "Re-tags owners after the directory changes; never overwrites a manual pick.",
  log_inbound_capture: "Writes a `transcript_sync_log` row (direction inbound) linked to the meeting a webhook created.",
  looks_like_binary_text: "Detects a .docx/.pdf pasted as text: ZIP magic `PK\\x03\\x04` or >1 % replacement characters in the first 2,000 chars.",
  create_meeting: "Manual upload: validate → insert meeting → extract → commit → auto-sync to the sheet → 201 with counts and errors.",
  get_meeting: "One meeting (owner-scoped) with its commitments and overdue flags.",
  extract_commitments_for_meeting: "Re-runs extraction on a saved transcript; only new sentences are added.",
  seed_demo: "Creates sample meetings and commitments from the fixed `DEMO_MEETINGS` list (no ML).",
  with_overdue_flag: "Adds `is_overdue` at read time (due date passed and not done) — overdue is never stored.",
  list_commitments: "The account's commitments across meetings, with optional status/meeting filters.",
  create_commitment: "Adds a task by hand to one of the account's meetings; tags the owner; triggers auto-sync.",
  get_commitment_or_404: "Fetches a commitment only if its meeting belongs to the email — the ownership check used by every per-task route.",
  get_commitment: "Task detail: nudge history, tracker history, owner candidates for the dropdown.",
  update_commitment: "PATCH status and/or owner_email (a manual owner is marked `manual`), then mirrors a status change to the sheet.",
  send_test_nudge: "Sends a real reminder for one task now over one configured channel.",
  send_and_log: "Delivers one reminder through `notifications.deliver` and records it in `nudge_log`.",
  due_nudges: "Builds the list of (channel, task, reason) reminders due now for one account (cadence + 20 h repeat rule).",
  run_nudges: "Sends every due reminder for one account and returns the results.",
  nudge_scheduler_loop: "Background loop: sleep, find accounts with automatic nudges on, run their nudges; never dies.",
  start_nudge_scheduler: "Starts the daemon thread with the configured interval.",
  _mask: "Turns a secret into `••••` + last 4 characters for display.",
  list_nudge_settings: "The account's channels with secrets masked and the SMTP password replaced by `has_password`.",
  save_nudge_settings: "Validates and upserts one channel; a blank secret field keeps the saved one; Teams must be https.",
  delete_nudge_setting: "Removes one channel.",
  test_nudge_channel: "Sends a sample reminder to the signed-in account's own address.",
  nudge_preferences: "GET/POST the account's automatic-nudges switch (off by default).",
  run_nudges_now: "Sends whatever is due right now, regardless of the automatic switch.",
  nudge_activity: "Recent `nudge_log` rows for the account's tasks.",
  get_tracker_settings: "Returns the tracker provider and its config (⚠ config is not masked).",
  save_tracker_settings: "Validates required fields per provider and upserts the account's tracker settings.",
  sync_tracker: "Per-task \"Sync\": Google Sheets is real (append or update the row), Jira/Custom only log.",
  get_sheets_tracker_config: "The account's Sheets config, or None unless Sheets is the enabled provider.",
  push_unsynced_to_sheet: "Writes every not-yet-synced task in one API call, stores `row-n` refs and logs; the caller must hold the write lock.",
  auto_sync_to_sheet: "After a write: if auto-sync is on, takes the lock and pushes unsynced tasks. Returns (count, error); never raises.",
  auto_push_status_to_sheet: "Mirrors an in-app status change into column E of the task's row.",
  sync_all_to_tracker: "\"Sync all\" button: pushes the whole backlog even if auto-sync is off.",
  normalize_tracker_status: "Maps a typed status (\"Closed\", \"In Progress\", \"to-do\") to pending / in_progress / done, or None.",
  apply_inbound_tracker_update: "Applies a status that came from the tracker and logs it as inbound.",
  simulate_inbound_update: "UI button that exercises the inbound path without a real tracker.",
  tracker_webhook: "Real inbound endpoint for Apps Script/Jira: finds the task by owner email + external_ref. ⚠ No secret.",
  get_or_create_webhook_token: "Returns the account's capture token, creating one with `secrets.token_urlsafe(24)` if needed.",
  get_transcript_source_settings: "Returns the transcript source config and webhook token (⚠ config incl. Fathom secret not masked).",
  save_transcript_source_settings: "Validates required fields per provider and upserts; keeps the existing token.",
  regenerate_transcript_webhook_token: "Rotates the token — old webhook URLs stop working immediately.",
  check_transcript_source: "Mock \"Check now\": writes a log line only.",
  transcript_source_log: "Recent transcript sync/capture log rows for the account.",
  transcript_capture_webhook: "Generic real inbound endpoint: token → create meeting → extract → sync.",
  verify_fathom_signature: "Svix-style check: HMAC-SHA256 of `id.timestamp.body` with the whsec secret, compared with `hmac.compare_digest`.",
  _fathom_speaker_turns: "Pulls (speaker, text) turns out of Fathom's payload defensively; returns [] on an unknown shape.",
  fathom_webhook: "Real Fathom endpoint: token → provider check → signature → turns → meeting → extract → sync.",
  simulate_incoming_capture: "UI button that runs the capture-webhook code path with a sample transcript.",
  _template_setup_steps: "Generic \"how to get these credentials\" steps used when no Claude key is set.",
  _ai_setup_steps: "Asks Claude (via `ANTHROPIC_API_KEY`) for setup steps for a custom app; None on any failure.",
  suggest_setup_steps: "Route that returns AI or template setup steps with a disclaimer.",
  // transcript_parser
  normalize_text: "Replaces curly quotes and non-breaking spaces so the regexes see plain ASCII.",
  ModelNotReadyError: "Raised when `ml/action_detector.keras` is missing; app.py turns it into `extraction_error`.",
  _get_nlp: "Lazily loads spaCy `en_core_web_sm` (sentence splitter).",
  _get_embedder: "Lazily loads the MiniLM sentence-embedding model (downloads it on first ever use).",
  _get_model: "Lazily loads the Keras classifier; raises ModelNotReadyError if the file is missing.",
  to_turns: "Splits raw transcript text into (speaker, text) turns by classifying each line (see 5.2).",
  to_utterances: "Splits turns into sentences, breaks >40-word ASR sentences at discourse words, drops pieces under 4 words.",
  owner_of: "Decides who owns a sentence: a named roster member before will/to/should, else the speaker for I'll/I will, else UNRESOLVED for can you/please.",
  task_of: "Strips everything up to the first commitment cue word and trims to 120 characters.",
  due_of: "Returns the first due phrase (after by/before/on, or today/tomorrow/eod/asap/this week/next week).",
  resolve_due_date: "Turns a due phrase into an ISO date relative to the meeting date (weekdays and relative words only); None otherwise.",
  extract_commitments: "Full pipeline from raw text; returns dicts ready to insert.",
  extract_commitments_from_turns: "Pipeline from turns: sentences → embeddings (batch 128) → probabilities → keep ≥ threshold → rules.",
  // ai_extraction
  AIExtractionError: "Any engine failure; its message is shown to the user and triggers fallback.",
  _now: "UTC timestamp helper.",
  ensure_schema: "Creates the three ai_extraction tables if missing.",
  get_settings: "Account's engine, per-engine configs and consent time (defaults to built-in).",
  _masked: "Copy of a config with the API key masked for the browser.",
  _model_for: "The model to call for an engine: saved value, `ANTHROPIC_MODEL`, or the default.",
  _user_message: "Builds the prompt body: meeting date + whole transcript.",
  _call_anthropic: "Claude call with JSON-schema structured output, 16,000 max tokens, 300 s timeout; maps SDK errors to friendly messages.",
  _call_openai_compatible: "POST `{base_url}/chat/completions` with `response_format: json_object`; sends both Bearer and `api-key` headers.",
  _parse_items: "Validates the model's JSON into the parser's dict shape (ISO due dates, confidence clamped 0–1, task ≤300 chars).",
  run_engine: "Dispatches to the right engine; returns (items, model_used).",
  try_extract: "Hook used by app.py: returns items when an AI engine is chosen, consented and works; otherwise None (fallback). Commits the caller's transaction before the network call.",
  try_extract_from_turns: "Same, joining Fathom turns into `Speaker: text` lines first.",
  _log_run: "Records each AI attempt (ok / fell_back) in `ai_extraction_runs`; never raises.",
  init_app: "Registers the page and API routes below on the Flask app and creates the tables.",
  unauthorized: "Shared 401 response for the feature's routes.",
  page: "Serves `ai-extraction.html`.", status: "Whether the feature is available and which engines exist.",
  read_settings: "The account's AI settings with keys masked.",
  save_settings: "Saves engine + config; requires consent before an external engine is used; blank key keeps the saved one.",
  test_connection: "Runs the saved engine on a 4-line sample transcript.",
  recent_runs: "Recent AI runs for the account.",
  compare: "Runs both engines on a saved meeting and stores the results as a comparison (meeting untouched).",
  save_verdicts: "Stores a reviewer's real/not-real verdicts for a comparison.",
  scoreboard: "Precision per engine across reviewed comparisons.",
  // people_directory
  _norm: "Lower-cases and strips punctuation for name comparison.",
  _username_forms: "Accepts `DOMAIN\\\\jsmith`, `jsmith@corp` and `jsmith` as the same user name.",
  parse_csv: "Reads the directory CSV with flexible headers; returns (people, row errors); ValueError if there's no email column.",
  match_owner: "Name → email in three tiers (email/user name, full name, single first/last name). Returns auto, ambiguous (never guesses) or none.",
  full: "Inner helper: a person's normalised full name.",
  // notifications
  compose: "Subject and plain-text body for one reminder.",
  _payload: "JSON body shared by Teams/custom webhooks.",
  _post_json: "POSTs JSON with a 15 s timeout; returns (status, detail).",
  send_email: "SMTP with STARTTLS (587) or SSL (465), verified certificates, optional login, to the owner's email.",
  send_slack: "Bot token → look up the owner by email and DM them; or incoming webhook → post in a channel.",
  send_teams: "Posts an Adaptive Card to a Teams Workflows URL (https only), plus recipient fields for custom flows.",
  send_custom: "POSTs the reminder JSON to any URL.",
  deliver: "Picks the sender for the channel; catches everything and returns (sent|failed|skipped, detail).",
  // google_sheets
  GoogleAuthNotReadyError: "Raised when the client secret or `google_token.json` is missing.",
  find_client_secret_path: "Finds `client_secret_*.json` in the project folder.",
  get_credentials: "Loads the cached OAuth token and refreshes it silently, rewriting the file.",
  _values_api: "Builds the Sheets v4 `spreadsheets().values()` client.",
  status_label: "pending → Pending, in_progress → In progress, done → Done.",
  append_commitment_rows: "Finds the next empty row via column A, writes the header if needed, writes A{n}:E{m}; returns `row-n` refs.",
  append_commitment_row: "Single-row wrapper.",
  update_status: "Overwrites column E of a known row.",
  main: "Script entry point (see the module's docstring).",
  // ml scripts
  load: "Reads a labelled CSV into `speaker: text` strings and 0/1 labels.",
  embed: "Embeds texts with MiniLM (batch 256), caching the matrix as .npy.",
  docx_text: "Reads paragraphs from a .docx transcript.",
  reflowed_vtt_text: "Repairs the lecture's damaged WebVTT line breaks.",
  split_corpus: "Splits the 180-meeting corpus file into one block per meeting.",
  sources: "Yields (family, source, text) for every transcript file.",
  mask_names: "Replaces names with placeholders so training doesn't learn specific names.",
  set_seeds: "Fixes Python/NumPy/TensorFlow seeds so runs are repeatable.",
  build_model: "The 256→Dropout→128→1 MLP.",
  train: "Trains with sample weights and early stopping.",
  scores: "Precision, recall, F1 and counts at a threshold.",
  best_threshold: "Picks the threshold with the best F1 on a validation slice.",
};
const MAINS = {
  "google_oauth_setup.py": "One-time OAuth consent: opens a browser (`InstalledAppFlow.run_local_server`) and writes `google_token.json`.",
  "ml/train_action_detector.py": "Trains the original model from the balanced CSVs and saves `ml/action_detector.keras`.",
  "ml/prepare_transcript_dataset.py": "Builds `utterances_for_labelling.csv` from the team's transcripts.",
  "ml/retrain_with_transcripts.py": "Leave-one-family-out evaluation, final training, report — and auto-deploy if macro-F1 improves (⚠).",
};
const GROUPS_APP = [
  ["Core helpers", ["get_db", "init_db", "migrate_db", "now_iso", "today_str", "current_email", "require_email"]],
  ["Pages", ["index", "dashboard_page", "settings_page", "people_page"]],
  ["People directory", ["_person_fields", "_after_directory_change", "list_people", "people_template", "upload_people", "add_person", "change_person", "load_people", "load_people_for_meeting", "rematch_owners"]],
  ["Sign-in & version", ["version", "identify", "me", "logout"]],
  ["Meetings & extraction", ["list_meetings", "create_meeting_row", "auto_extract_commitments", "auto_extract_commitments_from_turns", "_insert_extracted_commitments", "log_inbound_capture", "looks_like_binary_text", "create_meeting", "get_meeting", "extract_commitments_for_meeting", "seed_demo"]],
  ["Commitments", ["with_overdue_flag", "list_commitments", "create_commitment", "get_commitment_or_404", "get_commitment", "update_commitment"]],
  ["Nudges", ["send_test_nudge", "send_and_log", "due_nudges", "run_nudges", "nudge_scheduler_loop", "start_nudge_scheduler", "_mask", "list_nudge_settings", "save_nudge_settings", "delete_nudge_setting", "test_nudge_channel", "nudge_preferences", "run_nudges_now", "nudge_activity"]],
  ["Tracker (Google Sheets / Jira mock)", ["get_tracker_settings", "save_tracker_settings", "sync_tracker", "get_sheets_tracker_config", "push_unsynced_to_sheet", "auto_sync_to_sheet", "auto_push_status_to_sheet", "sync_all_to_tracker", "normalize_tracker_status", "apply_inbound_tracker_update", "simulate_inbound_update", "tracker_webhook"]],
  ["Transcript sources & webhooks", ["get_or_create_webhook_token", "get_transcript_source_settings", "save_transcript_source_settings", "regenerate_transcript_webhook_token", "check_transcript_source", "transcript_source_log", "transcript_capture_webhook", "verify_fathom_signature", "_fathom_speaker_turns", "fathom_webhook", "simulate_incoming_capture"]],
  ["Setup-steps helper", ["_template_setup_steps", "_ai_setup_steps", "suggest_setup_steps"]],
];
const missing = [];
function refRows(file, names) {
  const items = INVENTORY[file];
  const pick = names ? names.map((n) => items.find((i) => i.name === n)) : items;
  return pick.map((it) => {
    let d = DESC[it.name];
    if (it.name === "main") d = MAINS[file];
    if (!d) missing.push(`${file}:${it.name}`);
    const kind = it.kind === "class" ? "class " : "";
    const sig = `${kind}${it.name}(${it.args.filter((a) => a !== "self").join(", ")})`;
    const route = it.routes.length ? it.routes.join(" ") + `  [${it.auth}]` : "—";
    return [`\`${sig}\`  :${it.line}`, route, d || ""];
  });
}
add(h1("3  Function and class reference", "s3"),
  p("Every function and class in the Python code, grouped by module and by feature, with its line number, the route that calls it (if any) " +
    "and what it does. The **Auth** in brackets is how a route identifies the caller: `session` (signed cookie), `URL token`, " +
    "`token + HMAC signature`, or `none`. 157 definitions in total: 154 functions (54 of them routes) and 3 exception classes."),
  ...callout("tip", "Reading order for the code", ["`schema.sql` → `get_db` → `create_meeting` → `auto_extract_commitments` → `transcript_parser.extract_commitments_from_turns` → " +
    "`_insert_extracted_commitments` → `people_directory.match_owner` → `auto_sync_to_sheet`. That chain is 80 % of what the app is."]),
  h2("3.1  app.py — routes and glue", "r_app"),
);
GROUPS_APP.forEach(([title, names]) => add(h3(title), ...table(["Function  :line", "Route  [auth]", "What it does"], refRows("app.py", names), [30, 24, 46], { size: 17 })));
const allApp = new Set(GROUPS_APP.flatMap((g) => g[1]));
INVENTORY["app.py"].forEach((i) => { if (!allApp.has(i.name)) missing.push("ungrouped app.py:" + i.name); });
add(h2("3.2  transcript_parser.py — the built-in extractor", "r_tp"),
  p("Pure functions plus three lazily-loaded models. Constants worth knowing: `DEFAULT_THRESHOLD = 0.90`, `MODEL_PATH = ml/action_detector.keras`, " +
    "`EMBEDDING_MODEL_NAME = all-MiniLM-L6-v2`, and the regexes `HEADER`, `TS_HEADER`, `TS_INLINE`, `INLINE`, `CUE_SCAFFOLD`, `SECTION_HEADING`, " +
    "`SELF`, `OTHER`, `YOU`, `CUE`, `DUE`, `SPLIT_AT` (explained in section 5)."),
  ...table(["Function  :line", "Route", "What it does"], refRows("transcript_parser.py"), [30, 10, 60], { size: 17 }),
  h2("3.3  ai_extraction.py — optional LLM engine", "r_ai"),
  p("Self-contained feature (v0.5.0). Everything between the `[ai-extraction v0.5.0]` markers in app.py plus this file can be removed to revert it " +
    "(`_snapshots/v0.4.0/revert_ai_extraction.py` does exactly that). Route functions are nested inside `init_app`."),
  ...table(["Function  :line", "Route  [auth]", "What it does"], refRows("ai_extraction.py"), [30, 24, 46], { size: 17 }),
  h2("3.4  people_directory.py, notifications.py, google_sheets.py"),
  ...table(["Function  :line", "Module", "What it does"], [
    ...refRows("people_directory.py").map((r) => [r[0], "people_directory", r[2]]),
    ...refRows("notifications.py").map((r) => [r[0], "notifications", r[2]]),
    ...refRows("google_sheets.py").map((r) => [r[0], "google_sheets", r[2]]),
    ...refRows("google_oauth_setup.py").map((r) => [r[0], "google_oauth_setup", r[2]]),
  ], [30, 16, 54], { size: 17 }),
  h2("3.5  Offline ML scripts (ml/)"),
  ...table(["Function  :line", "Script", "What it does"], [
    ...["ml/train_action_detector.py", "ml/prepare_transcript_dataset.py", "ml/retrain_with_transcripts.py"].flatMap((f) =>
      refRows(f).map((r) => [r[0], path.basename(f), r[2]])),
  ], [30, 26, 44], { size: 17 }),
  h2("3.6  Frontend helpers (static/app.js)"),
  ...table(["Function", "What it does"], [
    ["`api(path, options)`", "fetch() wrapper: JSON in/out; throws `Error(data.error)` on a non-2xx response so callers can `toast(e.message)`."],
    ["`toast(message)`", "Shows a 2.6 s notification using `textContent` (safe for any text)."],
    ["`escapeHtml(str)`", "Escapes text for use inside `innerHTML` by round-tripping through a div's textContent. Use it for every server value."],
    ["`formatDate` · `formatDateTime`", "Display formatting; treats stored UTC timestamps as UTC."],
    ["`requireIdentified()`", "Redirects to the sign-in page if `/api/me` returns 401; returns the email."],
    ["`initNav()` and theme functions", "Highlights the current page, shows the version, adds the AI-extraction link when that feature is on, light/dark theme."],
  ], [28, 72]),
);
if (missing.length) { console.error("Missing descriptions:", missing); process.exit(1); }

// ---------- 4. encryption
add(h1("4  Encryption and cryptography", "s4"),
  p("Short answer for the new owner: **this application does not encrypt anything itself.** It relies on TLS for data in transit to outside " +
    "services, uses HMAC signatures (not encryption) for its session cookie and the Fathom webhook, and stores every secret in plain text on " +
    "the local disk. That is acceptable for a single-user laptop prototype and is the first thing to change before it serves anyone else."),
  h2("4.1  Encryption vs. signing vs. hashing — 60-second primer"),
  ...table(["Term", "Question it answers", "Used here?"], [
    ["Encryption (e.g. AES, TLS)", "Can anyone else read this?", "Only via TLS, done by libraries — never by app code"],
    ["Signature / MAC (e.g. HMAC-SHA256)", "Did someone with the key produce exactly this content?", "Yes: session cookie (itsdangerous) and Fathom webhooks"],
    ["Hashing (e.g. SHA-256, bcrypt)", "One-way fingerprint — same input, same output", "Only inside HMAC; no passwords exist to hash"],
    ["Random token", "Hard-to-guess identifier", "Yes: `secrets.token_urlsafe(24)` for webhook URLs (192 bits)"],
  ], [28, 40, 32]),
  h2("4.2  What protects each piece of data"),
  ...table(["Data / channel", "Protection", "Mechanism and code", "Status"], [
    ["Browser ↔ app on localhost", "None needed locally", "Plain HTTP on 127.0.0.1:5000", "✔ locally · ✖ if exposed"],
    ["Session cookie `{email}`", "Integrity, not secrecy", "Flask signs it with HMAC using `app.secret_key` (hard-coded in app.py). Readable by anyone holding the cookie.", "⚠ key hard-coded"],
    ["Internet caller → ngrok", "TLS", "ngrok terminates https; ngrok → localhost is plain HTTP on the same machine", "✔"],
    ["Fathom webhook body", "Authenticity", "`verify_fathom_signature`: HMAC-SHA256(secret, `id.timestamp.body`), base64, `hmac.compare_digest` (constant-time)", "✔ · ⚠ no replay window"],
    ["Generic capture webhook", "Secret URL", "Unguessable token in the path; no body signature", "⚠"],
    ["Tracker webhook", "None", "email + `row-n` in the JSON body", "✖"],
    ["App → Google Sheets", "TLS + OAuth 2.0 bearer token", "google-api-python-client over HTTPS; token auto-refreshed", "✔"],
    ["App → Claude / OpenAI-compatible", "TLS + API key header", "Anthropic SDK / `urllib` HTTPS; the full transcript leaves the machine", "✔ (+ consent gate)"],
    ["App → SMTP server", "TLS", "`starttls(context=ssl.create_default_context())` on 587 or `SMTP_SSL` on 465 — certificates verified", "✔"],
    ["App → Slack / Teams / custom", "TLS", "HTTPS URLs; Teams URLs must start with https://", "✔ (custom may be http)"],
    ["Secrets at rest (DB, token files)", "None", "Plain text in `commitment_tracker.db` and `google_token.json`", "✖"],
    ["Transcripts and tasks at rest", "None", "Full transcript text in `meetings.transcript_excerpt`", "✖ (OS disk encryption only)"],
  ], [22, 16, 46, 16], { size: 17 }),
  ...code([
    "secret_bytes = base64.b64decode(secret.split(\"_\", 1)[1]) if secret.startswith(\"whsec_\") else secret.encode()",
    "signed_content = f\"{webhook_id}.{webhook_timestamp}.{req.get_data(as_text=True)}\"",
    "expected = base64.b64encode(hmac.new(secret_bytes, signed_content.encode(), hashlib.sha256).digest()).decode()",
    "for part in signature_header.split():            # header may hold several \"v1,<sig>\" values",
    "    sig = part.split(\",\", 1)[1] if \",\" in part else part",
    "    if hmac.compare_digest(sig, expected):        # constant-time compare: no timing leak",
    "        return True",
  ], "app.py:1870 verify_fathom_signature — the only cryptographic check written in app code"),
  p(`${seeFig(8, "f_hooks", "webhook verification")} compares the three inbound webhooks side by side.`),
  h2("4.3  Recommended changes, in order"),
  ...steps([
    "Read `app.secret_key` from an environment variable (generate with `python -c \"import secrets; print(secrets.token_hex(32))\"`). Changing it signs everyone out — harmless here.",
    "Turn on OS disk encryption (BitLocker) on any machine holding the .db or token files; keep backups encrypted.",
    "Encrypt secret columns with a key outside the DB — e.g. `cryptography.fernet.Fernet` with the key in Windows Credential Manager via `keyring`. Touches the save/get settings routes and every reader of `config_json` / `webhook_or_key`.",
    "Reject Fathom requests whose `webhook-timestamp` is more than 5 minutes old (stops replays).",
    "Add a shared secret header to the tracker webhook and the Apps Script snippet.",
    "If ever deployed: HTTPS in front of a real WSGI server, `SESSION_COOKIE_SECURE=True`, `SESSION_COOKIE_HTTPONLY` (Flask default) and `SAMESITE=Lax`.",
  ]),
);
figure("08_webhooks.png", "How each inbound webhook decides whether to trust a request", "each column is one endpoint, top to bottom in code order. " +
  "A red box is the HTTP status returned when that check fails. The notes underneath give the residual risk.", "f_hooks");

// ---------- 5. chunking
add(h1("5  How chunking works", "s5"),
  p("\"Chunking\" in this project means splitting a long transcript into pieces small enough to classify one at a time. There is no vector database and " +
    "no retrieval (RAG) here, so there are no overlapping token windows. The built-in model classifies **one sentence at a time**; " +
    "the LLM engine sends **the whole transcript in one request**. Here is every level of splitting, with the rule and the reason:"),
  ...table(["Level", "Where", "Rule / size", "Why"], [
    ["Lines", "`to_turns` (`normalize_text(raw).splitlines()`)", "Each non-blank line, after curly quotes are fixed", "Transcript exports are line-oriented"],
    ["Speaker turns", "`to_turns`", "A header line (`[10:00 AM] Name:`, `0:00 - Name`) starts a turn; `Name: text` or `[09:00] Name: text` is a turn on one line; plain lines are appended to the current turn; WebVTT cue numbers/timings are skipped; `Date:`/`Participants:` style labels are dropped; an ALL-CAPS heading starts an UNKNOWN turn", "Ownership needs the speaker"],
    ["Sentences", "`to_utterances` → spaCy `nlp(text).sents`", "Linguistic sentence boundaries", "One commitment ≈ one sentence"],
    ["Long-sentence split", "`to_utterances` with `SPLIT_AT`", "If a sentence has > 40 words, split before so / and then / but / because / okay / now / then / also", "Speech-to-text output has no punctuation, so spaCy returns 200-word \"sentences\""],
    ["Noise filter", "`to_utterances`", "Drop pieces with < 4 words", "\"Okay sure.\", \"Thanks, Bhuvan.\" are never commitments"],
    ["Embedding batches", "`embedder.encode(..., batch_size=128)`", "128 sentences per forward pass (256 in training)", "Memory/speed only — results are identical"],
    ["Task text", "`task_of`", "Cut at 120 characters", "Keeps the sheet readable (⚠ can cut mid-word)"],
    ["LLM request", "`_user_message` in ai_extraction.py", "Whole transcript, max_tokens 16,000, 300 s timeout — no chunking", "Keeps cross-sentence context; a cut-off reply raises and falls back to the built-in model"],
    ["Sheets writes", "`append_commitment_rows`", "All unsynced rows in one `values.update`", "One API call, consistent row numbers"],
  ], [14, 24, 40, 22], { size: 17 }),
  p(`${seeFig(3, "f_pipe", "extraction pipeline")} draws the stages and then follows two real lines of the FY2027 transcript through them, with the actual model output.`),
  h2("5.1  The worked example in numbers"),
  ...code([
    "[11:28 AM] Sarah Xian:",
    "Understood, Bhuvan. I'll track everyone's deliverables from the 26th through the 29th, ... master feedback report on the 30th.",
    "[11:29 AM] Bhuvan:",
    "Once I receive Sarah's feedback on the 30th, I will immediately issue a Calendar invite by Friday.",
    "",
    "TURNS       2   (TS_HEADER matched both header lines)",
    "UTTERANCES  2   ('Understood, Bhuvan.' = 2 words < 4 -> dropped)",
    "EMB shape   (2, 384)",
    "PROBS       [1.0, 0.9999]          -> both >= 0.90",
    "COMMITMENT  owner 'Sarah Xian'  task 'track everyone's deliverables ... and give y'  due None",
    "COMMITMENT  owner 'Bhuvan'      task 'immediately issue a Calendar invite by Friday'  due None",
  ], "Output of the real pipeline, 2 Oct 2026 (script in section 11.3)"),
  ...callout("warn", "Three limitations this example exposes", [
    "**First due phrase wins:** `DUE.search` finds \"on the 30th\" before \"by Friday\", and `resolve_due_date` only understands weekdays and relative words, so the due date is lost. Fix: try every match (`finditer`) and add day-of-month parsing (\"the 30th\" → next date with day 30).",
    "**Hard 120-character cut** produces \"…and give y\". Fix: cut at the last word boundary.",
    "**Turn text spans lines:** a speaker's paragraph broken across lines is joined with spaces, which is right for prose but merges list items in written notes.",
  ]),
  h2("5.2  How to change chunking safely"),
  ...steps([
    "Add the new transcript shape as a check in `tests/test_transcript_parsing.py` first (copy an existing format test).",
    "Change the regex or rule in `transcript_parser.py`.",
    "Run `python tests/run_all.py parsing`, then the whole suite.",
    "Run the FY2027 transcript through the trace script (11.3) and compare the commitment list before and after — a small regex change can move dozens of sentences.",
  ]),
);
figure("03_extraction_pipeline.png", "Extraction pipeline and a real worked example", "the top row turns text into a probability per sentence; " +
  "the second row turns each kept sentence into a commitment row. The panel underneath is a real run, including the limitations it reveals.", "f_pipe");

// ---------- 6. databases
const TABLES = [
  ["users", "One row per email that ever signed in", "`identify`", "—"],
  ["meetings", "A meeting and its full transcript text (`transcript_excerpt` holds the whole text despite the name)", "upload, webhooks, demo", "dashboard, extraction"],
  ["commitments", "The tasks: text, owner name/email, match status, due date, status, confidence, quote, sheet row", "extraction, manual add, PATCH, inbound tracker", "almost everything"],
  ["people", "Per-account directory for owner matching (UNIQUE owner_email + email)", "People page", "`match_owner` via `load_people`"],
  ["nudge_settings", "One row per account × channel; secrets in `webhook_or_key` and `config_json`", "Settings", "scheduler, test buttons"],
  ["nudge_log", "Every reminder attempt with status, reason and detail", "`send_and_log`", "repeat rule, history UI"],
  ["nudge_preferences", "Automatic-nudges switch per account", "Settings", "scheduler loop"],
  ["tracker_settings", "Tracker provider + config (spreadsheet id, auto_sync)", "Settings", "sync functions"],
  ["tracker_sync_log", "Every push/receive with the tracker", "sync functions, webhook", "task history"],
  ["transcript_source_settings", "Transcript provider, config (Fathom secret), webhook token", "Settings", "webhooks"],
  ["transcript_sync_log", "Inbound captures and mock checks", "webhooks, check-now", "Integrations activity"],
  ["ai_extraction_settings", "Engine choice, per-engine config with API key, consent time", "AI page", "`try_extract`"],
  ["ai_extraction_runs", "Each AI attempt: ok or fell_back, count, detail", "`_log_run`", "AI page"],
  ["ai_extraction_comparisons", "Side-by-side engine results and reviewer verdicts", "compare", "scoreboard"],
];
add(h1("6  Databases and data access", "s6"),
  p("There is **one database**: a SQLite file, `commitment_tracker.db`, next to `app.py`. SQLite is a library, not a server — the file is the database, " +
    "and anyone who can read the file can read everything in it. There are 14 tables: 11 from `schema.sql` and 3 created by `ai_extraction.ensure_schema`. " +
    `${seeFig(4, "f_er", "entity relationships")} shows them all.`),
  ...table(["Table", "Holds", "Written by", "Read by"], TABLES, [20, 44, 18, 18], { size: 17, boldFirst: true }),
  h2("6.1  How the code talks to the database"),
  ...code([
    "def get_db():",
    "    conn = sqlite3.connect(DB_PATH, timeout=30)   # wait up to 30 s for a lock, then 'database is locked'",
    "    conn.row_factory = sqlite3.Row                 # row[\"title\"] instead of row[2]",
    "    conn.execute(\"PRAGMA foreign_keys = ON\")       # SQLite ignores FOREIGN KEY unless told per connection",
    "    return conn",
    "",
    "db = get_db()",
    "row = db.execute(\"SELECT ... WHERE meetings.owner_email = ? AND commitments.id = ?\", (email, commitment_id)).fetchone()",
    "db.execute(\"UPDATE commitments SET status = ? WHERE id = ?\", (status, commitment_id))",
    "db.commit()",
    "db.close()",
  ], "The pattern used in every route (app.py:118 and e.g. update_commitment)"),
  ...bullets([
    "**Always `?` placeholders** for values. Table/column names are never taken from user input (the only f-string SQL is in `migrate_db` with hard-coded table names).",
    "**Ownership is a WHERE clause:** every query on user data joins to `meetings.owner_email = ?` or filters `user_email = ?`. `get_commitment_or_404` returns None (→ 404) for someone else's task, so you can't even learn that it exists.",
    "**One connection per request, opened and closed explicitly.** No pool, no ORM. If you add an early `return`, close the connection first (look at existing routes — every error branch does).",
    "**JSON in TEXT columns:** settings live in `config_json` and are read with `json.loads`. They are not queryable by SQL — fine at this scale.",
    "**Timestamps** are ISO strings in UTC (`now_iso`); dates are `YYYY-MM-DD`. Overdue is computed when reading (`with_overdue_flag`), never stored.",
  ]),
  h2("6.2  Transactions and locking"),
  p("SQLite allows many readers but **one writer at a time**. Python's `sqlite3` opens a transaction automatically at the first INSERT/UPDATE and holds the write lock until `commit()`. Three places matter:"),
  ...table(["Where", "What it does", "Why"], [
    ["`auto_sync_to_sheet`, `sync_all_to_tracker`", "`BEGIN IMMEDIATE` before reading unsynced rows", "Takes the write lock up front so two syncs can't both send the same rows"],
    ["`ai_extraction.try_extract`", "`db.commit()` before the network call", "A slow LLM call must not hold the lock"],
    ["`create_meeting` (built-in engine)", "Meeting INSERT … ML … commitments INSERT … commit", "⚠ Lock held during ML inference — see 2.2"],
  ], [30, 34, 36]),
  h2("6.3  Changing the schema (migrations)"),
  p("There is no migration framework. The rule: **new databases get the column from `schema.sql`; existing databases get it from `migrate_db()`.** Both are needed."),
  ...steps([
    "Snapshot first: copy `commitment_tracker.db` (and any file you'll change) into `_snapshots/v<next>/`.",
    "Add the column to the CREATE TABLE in `schema.sql` (or a new `CREATE TABLE IF NOT EXISTS`).",
    "In `migrate_db()`, read `PRAGMA table_info(<table>)` and `ALTER TABLE … ADD COLUMN` if missing. SQLite can't add a UNIQUE or PRIMARY KEY column this way — enforce in code or rebuild the table.",
    "Add a check to `tests/test_people_and_nudges.py` section 6 (it upgrades a frozen 0.5.0 schema in `tests/fixtures/` and checks the migration runs twice safely).",
    "Restart the app (migrate runs at startup), bump `APP_VERSION`, add a CHANGELOG entry.",
  ]),
  h2("6.4  Other data stores"),
  ...table(["Store", "What", "Notes"], [
    ["`ml/action_detector.keras`", "Live classifier weights (1.6 MB)", "Identical to `_snapshots/v0.6.0/ml/` (verified by hash on 2 Oct 2026)"],
    ["`ml/action_detector_candidate.keras`", "Retrained model, not in use", "Pending a decision — section 10"],
    ["`ml/emb_cache/*.npy`", "Cached training embeddings", "Gitignored; delete to force re-embedding"],
    ["Hugging Face cache (user profile)", "MiniLM download", "First run needs internet"],
    ["The Google Sheet", "External copy of tasks (ID, Task, Owner, Due date, Status)", "Row number = link back (`external_ref`)"],
    ["`google_token.json`", "OAuth refresh token", "Credential — section 7"],
  ], [30, 34, 36]),
  h2("6.5  Looking inside the database"),
  ...code([
    "python -c \"import sqlite3; c=sqlite3.connect('commitment_tracker.db'); print([r[0] for r in c.execute('select name from sqlite_master where type=\\\"table\\\"')])\"",
    "",
    "-- in any SQLite tool (close it before running the app, it can hold locks):",
    "SELECT id, owner_name, owner_email, owner_match, due_date, status, external_ref FROM commitments ORDER BY id DESC LIMIT 20;",
    "SELECT triggered_at, channel, status, reason, detail FROM nudge_log ORDER BY id DESC LIMIT 20;",
    "SELECT triggered_at, provider, direction, status, detail FROM tracker_sync_log ORDER BY id DESC LIMIT 20;",
    "SELECT created_at, engine, model, status, commitments, detail FROM ai_extraction_runs ORDER BY id DESC LIMIT 10;",
  ]),
);
figure("04_database_er.png", "Database tables and relationships", "each box is a table with its main columns. Blue arrows are declared foreign keys; " +
  "✉ marks the email columns that scope data to an account; red 🔑 columns hold secrets in plain text.", "f_er");

// ---------- 7. credentials
add(h1("7  How credentials are maintained", "s7"),
  p(`There is no secrets manager. Credentials live in three places: **files in the project folder**, **rows in the SQLite database** and **environment variables**. ` +
    `${seeFig(7, "f_trust", "trust boundaries")} shows where each sits and what protects it.`),
  ...table(["Credential", "Stored in", "Created by", "Read by", "Sent to browser?", "How to rotate"], [
    ["Google OAuth client id/secret", "`client_secret_*.json` (gitignored)", "Download from Google Cloud console (Desktop app)", "`google_sheets.find_client_secret_path`", "No", "New client in the console, replace file, rerun setup"],
    ["Google refresh/access token", "`google_token.json` (gitignored)", "`python google_oauth_setup.py`", "`get_credentials` (auto-refresh, rewrites file)", "No", "Delete file + rerun setup; revoke at myaccount.google.com/permissions"],
    ["SMTP password (e.g. Gmail app password)", "`nudge_settings.config_json`", "Settings → Email", "`send_email`", "No (`has_password` only)", "Generate a new app password, save; blank field keeps old"],
    ["Slack bot token / webhook URLs (Slack, Teams, custom)", "`nudge_settings.webhook_or_key`", "Settings", "`notifications.deliver`", "Masked `••••abcd`", "Regenerate in Slack/Teams, save"],
    ["AI API keys (Anthropic / OpenAI-compatible)", "`ai_extraction_settings.config_json`", "AI extraction page", "`_call_*`", "Masked", "Create new key at the provider, save, delete the old one"],
    ["Fathom webhook secret (`whsec_…`)", "`transcript_source_settings.config_json`", "Settings → Fathom", "`verify_fathom_signature`", "⚠ Yes, unmasked", "Recreate the Fathom webhook, paste the new secret"],
    ["Webhook URL token", "`transcript_source_settings.webhook_token`", "`get_or_create_webhook_token`", "capture + Fathom webhooks", "Yes (needed to build the URL)", "\"Regenerate\" button"],
    ["Tracker config (spreadsheet id etc.)", "`tracker_settings.config_json`", "Settings", "sync functions", "⚠ Yes, unmasked", "—"],
    ["Flask session signing key", "Hard-coded `app.secret_key`", "—", "Flask", "No", "Move to env var (section 4.3)"],
    ["`ANTHROPIC_API_KEY`", "Environment variable", "You", "`_ai_setup_steps` only", "No", "Change env var, restart"],
  ], [17, 18, 16, 17, 13, 19], { size: 16 }),
  h2("7.1  Rules that are already followed — keep them"),
  ...bullets([
    "`.gitignore` excludes `*.db`, `client_secret*.json`, `google_token.json`, `_snapshots/`, `ml/emb_cache/`. Before any commit run `git status` and check none of these appear.",
    "Secrets are never written to logs or `detail` columns; error messages describe the failure (\"Anthropic rejected the API key\") without echoing the key.",
    "Saving a form with a blank secret field keeps the stored value, so the browser never needs the real secret back.",
    "Each account's secrets are only readable through routes that filter on that account's session email.",
    "The LLM engines require explicit consent (`consent_at`) before any transcript is sent to a third party.",
  ]),
  h2("7.2  Google OAuth specifics that bite"),
  ...bullets([
    "The OAuth consent screen is in **Testing** mode: only listed test users can authorise, and **refresh tokens expire after 7 days**. Symptom: `invalid_grant` in `tracker_sync_log`. Fix: rerun `python google_oauth_setup.py`, or publish the app.",
    "Scope is `https://www.googleapis.com/auth/spreadsheets` — read/write to **every** sheet that Google account can open, not just the tracker sheet. Treat `google_token.json` like a password.",
    "The Sheets API must be enabled in the same Google Cloud project as the client, or calls fail with 403.",
  ]),
  h2("7.3  Environment variables"),
  ...table(["Variable", "Default", "Effect"], [
    ["`AI_EXTRACTION_ENABLED`", "1", "`0` removes the AI-extraction page and routes entirely (v0.4.0 behaviour)"],
    ["`ANTHROPIC_API_KEY`", "unset", "Enables Claude-written setup steps for custom apps (not used for extraction — that key is per account)"],
    ["`ANTHROPIC_MODEL`", "`claude-opus-5`", "Default model for the Claude engine when the account hasn't chosen one"],
    ["`NUDGE_CHECK_INTERVAL_SECONDS`", "900", "How often the reminder thread wakes"],
    ["`WERKZEUG_RUN_MAIN`", "set by Flask", "Do not set by hand; marks the reloader child that runs the scheduler"],
  ], [32, 16, 52]),
);
figure("07_trust_boundaries.png", "Where credentials live and what protects them", "each dashed zone is a trust boundary. " +
  "✔ marks a control in place, ✖ a known gap. Anything inside 'Your PC' is only as safe as the PC and its backups.", "f_trust");

// ---------- 8. security
add(h1("8  Security measures and known gaps", "s8"),
  h2("8.1  Measures in place"),
  ...table(["Threat", "Measure", "Where in code", "Proven by test?"], [
    ["SQL injection", "Parameterised queries everywhere", "every `db.execute(..., (params))`", "Implicitly (all suites)"],
    ["Reading another account's data", "Ownership filter on every query; 404 not 403", "`get_commitment_or_404`, `owner_email = ?` filters", "Yes — test_people_and_nudges"],
    ["Cross-site scripting (stored)", "`escapeHtml()` on server text; `toast` uses textContent", "`static/*.html`, `app.js`", "Manual review (0.7.3 fixed 5 spots)"],
    ["Malicious / wrong uploads", "Extension check in browser + binary sniff on server", "`looks_like_binary_text`", "Yes — parsing suite"],
    ["Forged Fathom webhook", "HMAC-SHA256 with constant-time compare", "`verify_fathom_signature`", "Yes — valid / tampered / missing / empty secret"],
    ["Guessing a capture URL", "192-bit random token, rotatable", "`get_or_create_webhook_token`", "Yes — unknown token → 404"],
    ["Secret disclosure to browser", "Masking for nudge and AI secrets; blank = keep", "`_mask`, `_masked`, `has_password`", "Yes — 4c, B4"],
    ["Third-party outage breaking writes", "Never-raise wrappers; primary write committed first", "`deliver`, `try_extract`, `auto_sync_to_sheet`", "Yes — fallback tests"],
    ["Data sent to an LLM without agreement", "Consent gate", "`ai_extraction.save_settings`, `try_extract`", "Yes"],
    ["Duplicate rows / race", "`BEGIN IMMEDIATE`", "`auto_sync_to_sheet`", "Yes — two simultaneous syncs"],
    ["Wrong person nudged", "Ambiguous names are never auto-tagged", "`match_owner`", "Yes — 3c"],
    ["Credentials in git", ".gitignore entries", "`.gitignore`", "Check `git status`"],
  ], [20, 30, 30, 20], { size: 17 }),
  h2("8.2  Known gaps (ranked)"),
  ...table(["#", "Gap", "Why it matters", "Fix"], [
    ["S-1", "No authentication — any email signs in", "Anyone who can reach the app can act as anyone", "SSO (Google/Microsoft OIDC) or at least magic-link email"],
    ["S-2", "`debug=True` + ngrok", "The Werkzeug debugger allows code execution if reached publicly", "Never run ngrok with debug on; use `debug=False` or a production server"],
    ["S-3", "Tracker webhook has no secret", "Status of any synced task can be changed by guessing", "Shared secret header checked with `hmac.compare_digest`"],
    ["S-4", "Secrets unencrypted at rest", "A copy of the .db leaks every integration", "Fernet + keyring; BitLocker meanwhile"],
    ["S-5", "Hard-coded `secret_key`", "Anyone with the code can forge a session for any email", "Env var"],
    ["S-6", "Tracker & transcript settings returned unmasked", "Fathom whsec visible in the browser / to XSS", "Mask like `list_nudge_settings`"],
    ["S-7", "No CSRF protection (`get_json(force=True)` accepts any content type)", "Another site could post as a signed-in user", "Require `application/json` + SameSite=Lax/Strict cookie, or CSRF tokens"],
    ["S-8", "Custom webhook / base URL can be any address", "Server-side request forgery to internal services", "Allow-list https and block private IP ranges"],
    ["S-9", "Fathom replay", "A captured request can be replayed to duplicate a meeting", "Reject timestamps older than 5 minutes"],
    ["S-10", "No rate limiting", "Webhook and sign-in endpoints can be hammered", "flask-limiter or proxy limits"],
  ], [6, 28, 32, 34], { size: 17 }),
  ...callout("risk", "Checklist before exposing the app beyond one laptop", [
    "S-1, S-2, S-3 and S-5 fixed · served over HTTPS by a production WSGI server (waitress/gunicorn) · secrets encrypted or in a vault · " +
    "database backed up and encrypted · `python tests/run_all.py` green · the data-breach checks in 9.4 repeated against the deployed URL.",
  ]),
);

// ---------- 9. edge cases & testing
add(h1("9  Edge cases and testing", "s9"),
  h2("9.1  The test suite"),
  p("Tests are plain Python scripts — each check prints PASS/FAIL and the script exits non-zero if anything failed. No pytest, no network, no real Google " +
    "account: `_support.fresh_app()` points the app at a temporary database, and `FakeSheet` replaces the Google client."),
  ...code(["python tests/run_all.py            # all 4 suites, ~2–4 minutes (the parsing suite loads the ML models)",
    "python tests/run_all.py sheets     # only suites whose file name contains 'sheets'",
    "python tests/test_transcript_parsing.py   # run one suite directly to see every PASS line"]),
  ...table(["Suite", "Checks", "What it covers"], [
    ["`test_transcript_parsing.py`", "28", "All transcript formats (header-only, inline, `[09:00] Name: text`, WebVTT, numeric speakers, preamble labels, section headings), curly quotes, due-date resolution, binary-upload guard, Fathom signature valid/invalid/tampered/missing/empty, unknown token → 404"],
    ["`test_sheets_sync.py`", "15", "Auto-sync on/off, explicit A{n}:E{m} ranges, blank due date alignment, header row, concurrent syncs write once, status synonyms, inbound update not echoed back"],
    ["`test_people_and_nudges.py`", "54", "CSV parsing and errors, name-matching tiers, ambiguity, re-matching, manual override, replace mode; every nudge channel against fake SMTP / local receivers; cadence and repeat rules; secrets never returned; migration from a 0.5.0 database, twice"],
    ["`test_ai_extraction.py`", "22", "Settings and masking, consent, fallback on any failure, Fathom turns path, comparison leaves the meeting untouched, bad JSON, Anthropic request shape; one live call with a deliberately invalid key (skipped offline)"],
    ["**Total**", "**119**", "All passing at 0.7.3"],
  ], [28, 8, 64], { size: 17 }),
  h2("9.2  Edge-case catalogue"),
  ...table(["Area", "Edge case", "Expected behaviour", "Test?"], [
    ["Upload", ".docx/.pdf bytes submitted as text", "400 with \"convert it to .txt\"", "✔"],
    ["Upload", "Empty transcript / only scaffolding lines", "Meeting created, 0 commitments, no error", "✔"],
    ["Parsing", "Curly apostrophes (I’ll)", "Normalised; ownership still detected", "✔"],
    ["Parsing", "Speaker name is a number / a timestamp fragment", "Becomes UNKNOWN", "✔"],
    ["Parsing", "\"Date:\", \"Participants:\" preamble", "Dropped, not treated as speakers", "✔"],
    ["Parsing", "ASR text with no punctuation", "Split at discourse words once > 40 words", "Partly"],
    ["Parsing", "Two due phrases (\"on the 30th … by Friday\")", "⚠ first wins; date often None", "✖ add one"],
    ["Owners", "Two people share a first name", "`ambiguous`, no email, nobody nudged", "✔"],
    ["Owners", "\"Can you …\" with no name", "UNRESOLVED", "✔"],
    ["Owners", "Directory changes after extraction", "Auto/ambiguous/none re-matched; manual kept", "✔"],
    ["Model", "Model file missing", "Upload works; `extraction_error` explains", "✔"],
    ["AI", "Bad key, timeout, refusal, cut-off, invalid JSON", "Logged as fell_back; built-in result used", "✔"],
    ["Sheets", "Blank due date", "Columns stay aligned", "✔"],
    ["Sheets", "Two syncs at the same moment", "Rows written once", "✔"],
    ["Sheets", "Someone sorts/inserts rows in the sheet", "⚠ `row-n` links point at the wrong tasks", "✖ document/guard"],
    ["Sheets", "Token expired (7-day test mode)", "Upload kept; `tracker_sync_error` shown", "Partly"],
    ["Webhooks", "Unknown token / wrong provider / bad signature", "404 / 400 / 401", "✔"],
    ["Webhooks", "Fathom payload shape changes", "0 turns, logged, no 500", "Partly"],
    ["Nudges", "Task done / no due date / no owner email", "Not sent / skipped with reason", "✔"],
    ["Nudges", "Scheduler pass throws", "Printed, next pass runs", "✖"],
    ["Access", "Another account's task id in the URL", "404", "✔"],
    ["Migration", "Old DB from 0.5.0, run twice", "Columns added once, data intact", "✔"],
  ], [12, 34, 38, 16], { size: 17 }),
  h2("9.3  How to prove an edge case doesn't break the code"),
  ...steps([
    "Write the edge case as a failing check first in the right suite (pattern below).",
    "Fix the code until it passes.",
    "Run the **whole** suite — fixes in parsing often change owner/due results elsewhere.",
    "For extraction changes, also compare the FY2027 commitment list before/after (11.3). Tests prove rules; this proves quality didn't drop.",
    "Add the change to CHANGELOG.md with the test name.",
  ]),
  ...code([
    "from _support import fresh_app, ok      # puts the project on sys.path, temp DB per call",
    "",
    "app_module, client = fresh_app(\"alice@example.com\")      # signed-in test client",
    "r = client.post(\"/api/meetings\", json={\"title\": \"t\", \"transcript_excerpt\": \"PK\\x03\\x04...\"})",
    "ok(r.status_code == 400, \"binary upload rejected\")",
    "",
    "_, bob = fresh_app(\"bob@example.com\")",
    "ok(bob.get(\"/api/commitments/1\").status_code == 404, \"bob can't see alice's task\")",
  ], "Shape of a new check (see tests/_support.py for the exact helpers)"),
  h2("9.4  Data-breach checks — run after any change to routes, settings or the UI"),
  ...table(["Check", "How", "Pass when"], [
    ["No session → no data", "Private window: open `/api/commitments`, `/api/nudge-settings`, `/api/ai-extraction/settings`", "Every one returns 401"],
    ["Account isolation", "Sign in as B, request A's `/api/commitments/<id>`, `/api/meetings/<id>`, PATCH it", "404 for all, nothing changed"],
    ["Secrets stay server-side", "DevTools → Network → GET settings responses; search for `xoxb`, `whsec`, `sk-`, your SMTP password", "Only masked values (tracker/transcript config is a known exception — S-6)"],
    ["XSS", "Create a task / person / custom app named `<img src=x onerror=alert(1)>`, open every page", "Text shows literally, no alert"],
    ["Webhook trust", "POST to the Fathom URL with a wrong signature; to capture URL with a wrong token", "401 / 404, no meeting created"],
    ["Nothing secret in git", "`git status --ignored` and `git diff --stat`", "No .db, token, client_secret or snapshot files staged"],
    ["Nothing secret in logs", "Search the console output and `*_log.detail` columns for keys/passwords", "None found"],
    ["LLM consent", "Choose an AI engine without consenting, upload", "Built-in engine used; no outbound call (ai_extraction_runs has no row)"],
  ], [20, 52, 28], { size: 17 }),
);

// ---------- 10. ML primer
add(h1("10  ML primer for this project", "s10"),
  p("You don't need deep ML knowledge to maintain this. You need five ideas and the discipline to measure before deploying."),
  h2("10.1  The five ideas"),
  ...table(["Idea", "In this codebase", "Analogy"], [
    ["Embedding", "MiniLM turns `\"Bhuvan: I will send the deck by Friday\"` into 384 numbers; sentences with similar meaning get nearby vectors. It is a pre-trained, frozen model.", "A hash that preserves meaning instead of destroying it"],
    ["Classifier (MLP)", "A small network (384 → 256 → 128 → 1) trained to map an embedding to P(commitment). This is the only part we trained.", "A learned scoring function"],
    ["Threshold", "Keep sentences with P ≥ 0.90. Higher = fewer, surer results (precision); lower = catch more (recall).", "An alerting threshold"],
    ["Precision / recall / F1", "Precision: of what we flagged, how much was right. Recall: of the real commitments, how many we found. F1 balances both.", "False-positive vs false-negative rate"],
    ["Generalisation", "Doing well on training data proves little; we test on transcript *families* the model never saw.", "Testing on production-like data, not fixtures"],
  ], [18, 56, 26], { size: 17 }),
  h2("10.2  Where the model stands (honest numbers)"),
  ...table(["Held-out transcript family", "Current model F1 (P / R)", "Retrained candidate F1 (P / R)"], [
    ["FY2027 (the one real business meeting)", "**0.417** (0.29 / 0.77)", "0.381 (0.50 / 0.31)"],
    ["Template, numbered", "0.250 (0.15 / 0.68)", "0.627 (0.55 / 0.73)"],
    ["Template corpus", "0.279 (0.22 / 0.37)", "0.447 (0.48 / 0.42)"],
    ["Workshop — clustering", "0.194 (0.12 / 0.58)", "0.500 (1.00 / 0.33)"],
    ["Workshop — validation", "0.256 (0.15 / 1.00)", "0.640 (0.57 / 0.73)"],
    ["Real lecture recording", "0.027", "0.029"],
    ["**Average (macro-F1)**", "**0.237**", "**0.437**"],
  ], [40, 30, 30]),
  p("Reading this: the current model finds most real commitments (high recall) but buries them in false positives (low precision) — reviewers must prune. " +
    "The candidate is much cleaner on synthetic families but **misses most commitments in the real meeting**, so it was not deployed. The training data " +
    "is mostly template-generated (the 180-meeting corpus has only 273 distinct sentences); real transcripts are the bottleneck, not the algorithm."),
  ...callout("info", "When to use the LLM engine instead", "For accuracy today, an LLM (Claude or Gemini via the AI-extraction page) understands context, " +
    "dates (\"the 30th\") and implied owners far better. Use the **Compare** and **Scoreboard** features to measure it against the built-in model on your own " +
    "meetings before switching an account. Cost and data-sharing consent are the trade-offs."),
  h2("10.3  Retraining safely"),
  ...steps([
    "**Snapshot** `ml/action_detector.keras` into `_snapshots/v<next>/ml/` — `retrain_with_transcripts.py` overwrites it automatically when macro-F1 improves.",
    "Add transcripts → `python ml/prepare_transcript_dataset.py` → label new lines in `training_data/labels.py` (ACTION set) → regenerate `labelled_utterances.csv`.",
    "`python ml/retrain_with_transcripts.py` and read `ml/retrain_report.json` per family, not just the average. Real meetings matter most.",
    "If it was auto-deployed but a real family regressed, restore the snapshot (this happened on 2026-10-01).",
    "Run the full test suite and the FY2027 trace; note the decision and numbers in CHANGELOG.md.",
  ]),
);
figure("09_ml_training.png", "How the model was trained, and the retraining decision", "panel A is the original training that produced the model in use. " +
  "Panel B is the retraining run on the team's transcripts and why its result is parked.", "f_ml");

// ---------- 11. debug
add(h1("11  Diagnose and debug", "s11"),
  p(`Start with ${seeFig(11, "f_triage", "triage table")}: find the symptom, look where it says, apply the usual fix. The rest of this section gives the tools.`),
  h2("11.1  First five minutes on any problem"),
  ...steps([
    "Read the response in the browser: DevTools → Network → the failing request → Response. App errors are always `{\"error\": \"…\"}` and usually name the cause.",
    "Read the console where `python app.py` runs: Python tracebacks, `[nudges]` lines and Flask's request log (method, path, status) are all there.",
    "Check the relevant log table (11.4) — every integration writes a row with a `detail` column on success *and* failure.",
    "Confirm only one app is running: `netstat -ano | findstr :5000` (PowerShell: `Get-NetTCPConnection -LocalPort 5000`). Stale copies from earlier runs have caused \"my change didn't apply\" more than once.",
    "Reproduce in a test with `fresh_app()` — then the fix comes with a regression check.",
  ]),
  h2("11.2  Where to look"),
  ...table(["Source", "Shows", "How to open"], [
    ["App console", "Tracebacks, request log, scheduler messages", "The terminal running `python app.py`"],
    ["`nudge_log`", "Every reminder attempt: sent/failed/skipped + reason + detail", "Task detail panel, or SQL"],
    ["`tracker_sync_log`", "Sheet pushes and inbound updates", "Task detail panel, or SQL"],
    ["`transcript_sync_log`", "Webhook captures (with turn counts) and mock checks", "Settings → Integrations activity"],
    ["`ai_extraction_runs`", "AI attempts: ok / fell_back + reason", "AI extraction page → Recent runs"],
    ["Apps Script executions", "Whether the sheet trigger fired and what HTTP code it got", "Sheet → Extensions → Apps Script → Executions"],
    ["ngrok inspector", "Every request through the tunnel, with body and response; can replay", "http://127.0.0.1:4040"],
    ["Werkzeug debugger", "Interactive traceback in the browser on a 500", "Local only — never through ngrok (S-2)"],
  ], [24, 46, 30]),
  h2("11.3  Tracing the extraction pipeline"),
  p("When extraction gives surprising results, run the stages by hand to see which one went wrong. This is the script that produced the worked example in section 5:"),
  ...code([
    "import transcript_parser as tp",
    "raw = open(\"FY2027_Financial_Planning_Meeting_Transcript.txt\", encoding=\"utf-8\").read()",
    "turns = tp.to_turns(raw);       print(turns.speaker.value_counts())   # all UNKNOWN? format not recognised",
    "utts = tp.to_utterances(turns); print(len(utts))                       # far fewer than expected? <4-word filter",
    "emb = tp._get_embedder().encode((utts.speaker + \": \" + utts.text).tolist(), normalize_embeddings=True)",
    "utts[\"prob\"] = tp._get_model().predict(emb, verbose=0).ravel()",
    "print(utts.sort_values(\"prob\", ascending=False).head(30))           # what scored high, what just missed 0.90",
    "for c in tp.extract_commitments(raw, meeting_date=\"2026-09-23\"): print(c)",
  ], "Run from the project folder with PYTHONIOENCODING=utf-8 set if the console shows encoding errors"),
  h2("11.4  Debugging with VS Code"),
  ...bullets([
    "Set breakpoints and run app.py under the debugger with `use_reloader=False` (temporarily change `app.run`) — the reloader's second process otherwise ignores your breakpoints. Remember the scheduler only starts in the reloader child, so start it by hand if you need it.",
    "To debug one endpoint without the browser, call it from a test with `fresh_app()` and set a breakpoint in the route.",
    "To debug a webhook, replay the captured request from the ngrok inspector after setting the breakpoint.",
  ]),
  h2("11.5  Incidents from this project's history"),
  ...table(["Symptom", "Root cause", "Fix (version)"], [
    ["Uploaded .docx produced garbage tasks", "Binary file read as text", "Browser .txt-only check + `looks_like_binary_text` (0.2.x)"],
    ["Transcript produced 0 or wrong turns", "`[10:00 AM] Name:` and `[09:00] Name: text` formats not recognised", "`TS_HEADER`, `TS_INLINE` (0.2.x–0.4.x)"],
    ["\"I’ll\" not detected as a commitment owner", "Curly apostrophe from Word/Zoom", "`normalize_text` (0.4.x)"],
    ["Sheet rows shifted one column right", "`values.append` table detection broken by blank due date", "Explicit `A{n}:E{m}` ranges (0.3.x)"],
    ["Same task written to the sheet twice", "Two syncs racing", "`BEGIN IMMEDIATE` (0.3.x)"],
    ["Apps Script `TypeError … reading 'range'`", "Running `onStatusEdit` from the editor (no event object) or as a simple trigger", "Installable On-edit trigger; never run it by hand"],
    ["Sheet edits stopped arriving", "ngrok free URL changed after restart", "Update the URL in Apps Script / Fathom"],
    ["Google 403 / invalid_grant", "API not enabled, user not a test user, or 7-day test token expiry", "Enable Sheets API, add test user, rerun `google_oauth_setup.py`"],
    ["\"database is locked\"", "A second app copy or a DB viewer holding a write transaction", "Stop stale processes; close viewers"],
    ["Changes \"not applied\"", "An old Flask process still owning port 5000", "Kill stale PIDs, start once"],
    ["Model got worse after retraining", "Script auto-deployed on macro-F1 while FY2027 regressed", "Restored snapshot; candidate kept separately (0.7.0)"],
    ["PDF export produced no file", "Edge returns before writing; needed its own profile dir", "`docs/build_prd_pdf.py` polls and uses `--user-data-dir` (0.7.2)"],
  ], [30, 38, 32], { size: 17 }),
);
figure("11_debug_triage.png", "Triage table — symptom → where to look → usual fix", "find the row closest to what you see; " +
  "the middle column is the cheapest check, the right column the cause it usually reveals.", "f_triage");

// ---------- 12. conventions
add(h1("12  Working conventions", "s12"),
  ...table(["Convention", "What to do", "Why"], [
    ["Version + changelog", "Every change: bump `APP_VERSION` in app.py and add a CHANGELOG.md entry (Added / Changed / Fixed / Security / Known issues)", "The changelog is the project history while git isn't being used"],
    ["No git commits without approval", "Local `main` is 1 commit ahead of origin; everything since 0.2.0 is uncommitted. Don't commit or push until the owner decides what goes in.", "Owner's explicit instruction"],
    ["Snapshot before risky work", "Copy files you'll change into `_snapshots/v<version>/`", "Fast, reliable revert without git"],
    ["Revertible trial features", "Wrap glue code in `[feature vX]` … `[/feature vX]` markers, keep the logic in its own module, add an env switch", "AI extraction can be removed cleanly (`_snapshots/v0.4.0/revert_ai_extraction.py`)"],
    ["Tests with every change", "Add a check, run `python tests/run_all.py`", "119 checks keep regressions out"],
    ["Docs regenerate from scripts", "`python docs/make_status_diagram.py` · `python docs/build_prd_pdf.py` · `python docs/make_guide_diagrams.py` + `node docs/build_technical_guide.js <inventory.json>`", "Diagrams and PDFs never drift from their source"],
    ["Escape on output", "`escapeHtml()` for any server/user text placed in HTML", "XSS (section 8)"],
    ["Never-raise integrations", "External calls return (status, detail) or an error string", "One flaky service can't break the app"],
  ], [20, 52, 28], { size: 17 }),
  h2("12.1  Adding a feature — the checklist"),
  ...steps([
    "Snapshot the files you'll touch.",
    "Schema: `schema.sql` + `migrate_db()` (6.3).",
    "Backend: route with `require_email()`, input validation, `?` parameters, ownership filter, close the connection on every path.",
    "Frontend: call through `api()`, show errors with `toast(e.message)`, escape every value placed in HTML.",
    "Tests: happy path, bad input (400), no session (401), other account (404).",
    "Version bump + CHANGELOG entry; update readme/PRD if behaviour visible to users changed.",
    "Run the full suite and the data-breach checks in 9.4.",
  ]),
);

// ---------- 13. backlog
add(h1("13  Open decisions and backlog", "s13"),
  ...table(["Item", "Type", "Notes"], [
    ["Deploy the retrained candidate model?", "Decision (owner)", "Recommendation: no, until more real transcripts are labelled (section 10.2)"],
    ["Keep or revert the AI-extraction feature?", "Decision (team)", "Revert script ready; currently on, test account uses Gemini 2.5 Pro"],
    ["Review the 158 ACTION labels", "Data", "Labels drive every number in 10.2"],
    ["What to commit to git", "Decision (owner)", "Exclude .db, tokens, client secrets, snapshots (already gitignored)"],
    ["Real transcripts from the team", "Data", "Needed for meaningful retraining"],
    ["S-1 … S-10 security gaps", "Engineering", "Section 8.2, in that order"],
    ["Commit meeting before built-in extraction", "Engineering", "Removes the lock held during ML (2.2)"],
    ["Due-date parsing: all matches + calendar dates", "Engineering", "Section 5.1"],
    ["Word-boundary task truncation", "Engineering", "Section 5.1"],
    ["Guard `row-n` links against sheet sorting", "Engineering", "Store the task ID column as the key instead of the row number"],
    ["Update stale app.py module docstring", "Housekeeping", "Section 1.4"],
    ["Real Jira sync; real Zoom/Meet/Teams pull", "Feature", "Currently mocks"],
    ["Real sign-in (SSO)", "Feature", "Prerequisite for any shared deployment"],
  ], [42, 18, 40], { size: 17 }),
);

// ---------- appendices
const routeRows = [];
Object.entries(INVENTORY).forEach(([file, items]) => items.forEach((it) => it.routes.forEach((r) => {
  const [m, ...rest] = r.split(" ");
  routeRows.push([m, `\`${rest.join(" ")}\``, `\`${it.name}\` (${path.basename(file)}:${it.line})`, it.auth]);
})));
add(h1("Appendix A — Route catalogue", "appA"),
  p(`All ${routeRows.length} route functions, in source order. Auth: **session** = signed cookie from \`/api/identify\`; **URL token** = secret in the path; ` +
    "**token + HMAC signature** = token plus Fathom signature; **none** = open."),
  ...table(["Method", "Path", "Function", "Auth"], routeRows, [11, 43, 32, 14], { size: 16 }),
  h1("Appendix B — Tunable constants", "appB"),
  ...table(["Constant", "Value", "Where", "Effect"], [
    ["`DEFAULT_THRESHOLD`", "0.90", "transcript_parser.py", "Minimum probability to keep a sentence"],
    ["Long-sentence split", "> 40 words", "`to_utterances`", "When ASR sentences are split at discourse words"],
    ["Minimum utterance", "4 words", "`to_utterances`", "Shorter pieces are dropped"],
    ["Inference batch", "128", "`extract_commitments_from_turns`", "Embedding batch size"],
    ["Task length", "120 chars (built-in) / 300 (AI)", "`task_of` / `_parse_items`", "Truncation"],
    ["`NUDGE_REPEAT_WINDOW_HOURS`", "20", "app.py", "Minimum gap between attempts per task/channel/reason"],
    ["`NUDGE_CHECK_INTERVAL_SECONDS`", "900", "env", "Scheduler period"],
    ["`HTTP_TIMEOUT`", "15 s", "notifications.py", "Webhook/Slack/Teams timeout"],
    ["`REQUEST_TIMEOUT_SECONDS`", "300 s", "ai_extraction.py", "LLM call timeout"],
    ["`max_tokens`", "16,000", "`_call_anthropic`", "Longest Claude reply"],
    ["SQLite lock timeout", "30 s", "`get_db`", "Wait before \"database is locked\""],
    ["Binary-guard sample", "2,000 chars, 1 %", "`looks_like_binary_text`", "Replacement-character ratio"],
    ["Webhook token", "24 bytes (urlsafe)", "`get_or_create_webhook_token`", "Token length"],
    ["`NEW_DATA_WEIGHT`", "8", "retrain_with_transcripts.py", "Weight of each labelled transcript line vs a CSV row"],
  ], [28, 20, 26, 26], { size: 17 }),
  h1("Appendix C — Glossary", "appC"),
  ...table(["Term", "Meaning here"], [
    ["Commitment / task", "A promise extracted from a meeting: task text, owner, due date, status"],
    ["Turn", "A block of speech by one speaker"],
    ["Utterance", "One sentence (or ASR fragment) of a turn — the unit the classifier scores"],
    ["ACTION / DIALOG", "The two labels the classifier was trained on: a commitment vs ordinary talk"],
    ["Embedding", "Fixed-length vector of numbers representing a sentence's meaning (384 numbers here)"],
    ["MLP", "Multi-layer perceptron — a small fully-connected neural network"],
    ["Threshold", "Probability cut-off for keeping a sentence (0.90)"],
    ["Precision / recall / F1", "Share of flagged items that are right / share of real items found / their harmonic mean"],
    ["Family", "A group of near-duplicate transcripts, held out together during evaluation"],
    ["owner_match", "How an owner email was set: auto, ambiguous, none, manual (NULL = before 0.6.0)"],
    ["external_ref", "Link from a task to its tracker item — `row-<n>` for Google Sheets"],
    ["Nudge", "A reminder sent to a task owner"],
    ["Webhook", "An HTTP POST another service makes to us when something happens"],
    ["HMAC", "A keyed hash proving a message came from someone with the shared secret and wasn't changed"],
    ["ngrok", "A tunnel giving localhost a temporary public https URL"],
    ["Apps Script", "Google's JavaScript automation inside a Sheet; `onStatusEdit` posts edits to the app"],
    ["Snapshot", "Copies of files in `_snapshots/` taken before a risky change"],
    ["Fallback", "Using the built-in model when the AI engine fails"],
  ], [24, 76]),
  h2("List of figures"),
  ...figures.map(([n, t, a]) => new Paragraph({ children: [new InternalHyperlink({ anchor: a, children: [new TextRun({ text: `Figure ${n} — ${t}`, style: "Hyperlink" })] })], spacing: { after: 60 } })),
);
flush();

// ------------------------------------------------------------------ document
const doc = new Document({
  creator: "Commitment Tracker technical lead",
  title: "Commitment Tracker — Technical Handover Guide",
  description: `Developer handover guide for version ${VERSION}`,
  styles: {
    default: { document: { run: { font: FONT, size: 21, color: C.ink } } },
    paragraphStyles: [
      { id: "Heading1", name: "Heading 1", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 36, bold: true, color: C.headBg }, paragraph: { spacing: { before: 120, after: 200 }, outlineLevel: 0,
          border: { bottom: { style: BorderStyle.SINGLE, size: 12, color: C.blue, space: 4 } } } },
      { id: "Heading2", name: "Heading 2", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 27, bold: true, color: C.blue }, paragraph: { spacing: { before: 260, after: 120 }, outlineLevel: 1, keepNext: true } },
      { id: "Heading3", name: "Heading 3", basedOn: "Normal", next: "Normal", quickFormat: true,
        run: { size: 22, bold: true, color: C.green }, paragraph: { spacing: { before: 200, after: 80 }, outlineLevel: 2, keepNext: true } },
    ],
  },
  numbering: { config: [
    { reference: "bullets", levels: [0, 1, 2].map((l) => ({ level: l, format: LevelFormat.BULLET, text: ["•", "◦", "▪"][l], alignment: AlignmentType.LEFT,
      style: { paragraph: { indent: { left: 360 + l * 360, hanging: 260 } } } })) },
    { reference: "steps", levels: [{ level: 0, format: LevelFormat.DECIMAL, text: "%1.", alignment: AlignmentType.LEFT,
      style: { paragraph: { indent: { left: 400, hanging: 300 } } } }] },
  ] },
  sections,
});
Packer.toBuffer(doc).then((buf) => { fs.writeFileSync(OUT, buf); console.log("wrote", OUT, (buf.length / 1024).toFixed(0) + " KB,", sections.length, "sections,", figNo, "figures"); });

/* inventory.json is produced by a short AST script: for each .py file, every FunctionDef/ClassDef with
   {kind, name, line, args, routes: ["METHOD /path"], auth}. Re-create it whenever functions are added so section 3
   and Appendix A stay complete — the build fails loudly if a function has no description in DESC. */
