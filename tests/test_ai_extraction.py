"""Optional AI-extraction engine (0.5.0): settings, fallback to built-in, comparison,
parsing, Anthropic request shape. Check K calls the live Anthropic API with an
invalid key - free, and tolerated when offline."""

import sys, json, tempfile, threading, sqlite3
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from _support import ROOT, ok  # noqa: E402 (also puts the project on sys.path)
import app as appmod, ai_extraction as ax, transcript_parser as tp

tmp = Path(tempfile.mkdtemp()) / "t.db"; appmod.DB_PATH = tmp; appmod.init_db()
BUILTIN = [{"task_text": "builtin task", "owner_name": "B", "due_date": None, "confidence_score": 0.95, "source_quote": "builtin quote"}]
tp.extract_commitments = lambda text, meeting_date=None, threshold=0.9: list(BUILTIN)
tp.extract_commitments_from_turns = lambda turns, meeting_date=None, threshold=0.9: list(BUILTIN)
AI = [{"task_text": "ai task", "owner_name": "Kevin Heys", "due_date": "2026-09-27", "confidence_score": 0.9, "source_quote": "ai quote"}]
real_run_engine = ax.run_engine

c = appmod.app.test_client()
c.post("/api/identify", json={"email": "t@example.com"})
def tasks(mid): return [x["task_text"] for x in c.get(f"/api/meetings/{mid}").get_json()["commitments"]]
def upload(title): return c.post("/api/meetings", json={"title": title, "transcript_excerpt": "Priya: I will do it."}).get_json()

# A. default: built-in path, response shape unchanged
r = upload("A")
ok(tasks(r["id"]) == ["builtin task"] and set(r) == {"id", "commitments_extracted", "extraction_error", "synced_to_tracker", "tracker_sync_error"},
   f"A default uses built-in, response keys unchanged {sorted(r)}")

# B. settings validation + key masking/retention
ok(c.post("/api/ai-extraction/settings", json={"engine": "anthropic", "config": {}, "consent": True}).status_code == 400, "B1 key required")
ok(c.post("/api/ai-extraction/settings", json={"engine": "anthropic", "config": {"api_key": "sk-ant-secret1234"}}).status_code == 400, "B2 consent required")
ok(c.post("/api/ai-extraction/settings", json={"engine": "anthropic", "config": {"api_key": "sk-ant-secret1234"}, "consent": True}).status_code == 201, "B3 saves")
g = c.get("/api/ai-extraction/settings").get_json()
ok("secret" not in json.dumps(g) and g["configs"]["anthropic"]["api_key_hint"].endswith("1234"), f"B4 key masked in GET: {g['configs']['anthropic']}")
c.post("/api/ai-extraction/settings", json={"engine": "anthropic", "config": {"api_key": "", "model": "claude-sonnet-5"}, "consent": True})
stored = json.loads(sqlite3.connect(tmp).execute("select config_json from ai_extraction_settings").fetchone()[0])
ok(stored["anthropic"] == {"api_key": "sk-ant-secret1234", "model": "claude-sonnet-5"}, "B5 blank key keeps saved key")
ok(c.post("/api/ai-extraction/settings", json={"engine": "openai_compatible", "config": {"model": "x"}, "consent": True}).status_code == 400, "B6 openai needs base URL")

# C. switching to built-in keeps saved keys, engine goes back to built-in path
c.post("/api/ai-extraction/settings", json={"engine": "builtin"})
g = c.get("/api/ai-extraction/settings").get_json()
ok(g["engine"] == "builtin" and g["configs"]["anthropic"]["has_api_key"], "C1 switch to built-in keeps the key")
r = upload("C"); ok(tasks(r["id"]) == ["builtin task"], "C2 built-in engine -> built-in results")

# D. AI success -> AI items inserted instead of built-in; run logged ok
c.post("/api/ai-extraction/settings", json={"engine": "anthropic", "config": {"model": "claude-opus-5"}, "consent": True})
ax.run_engine = lambda e, cfg, text, d: (list(AI), "claude-opus-5")
r = upload("D"); ok(tasks(r["id"]) == ["ai task"] and r["commitments_extracted"] == 1, f"D1 AI engine used: {tasks(r['id'])}")
runs = c.get("/api/ai-extraction/runs").get_json(); ok(runs[0]["status"] == "ok" and runs[0]["commitments"] == 1, "D2 run logged ok")

# E. AI failure -> built-in fallback, logged
def boom(*a): raise ax.AIExtractionError("rate limited by Anthropic - try again shortly")
ax.run_engine = boom
r = upload("E"); runs = c.get("/api/ai-extraction/runs").get_json()
ok(tasks(r["id"]) == ["builtin task"] and runs[0]["status"] == "fell_back" and "rate limited" in runs[0]["detail"], f"E fallback: {runs[0]['detail']}")

# G. unexpected exception type also falls back (never breaks upload)
def crash(*a): raise ValueError("weird")
ax.run_engine = crash
r = upload("G"); ok(tasks(r["id"]) == ["builtin task"], "G unexpected error -> built-in")

# F. Fathom-style turns path
ax.run_engine = lambda e, cfg, text, d: (list(AI), "claude-opus-5") if "Priya: I will send it" in text else ([], "x")
db = appmod.get_db(); mid = appmod.create_meeting_row(db, "t@example.com", "F", "2026-09-24", "x", source="live")
n, err = appmod.auto_extract_commitments_from_turns(db, mid, "2026-09-24", [{"speaker": "Priya", "text": "I will send it"}]); db.commit(); db.close()
ok(n == 1 and tasks(mid) == ["ai task"], "F speaker-turns path uses AI engine")

# H. compare (read-only for meeting) + verdicts + scoreboard
before = tasks(r["id"])
ax.run_engine = lambda e, cfg, text, d: (list(AI) + [dict(AI[0], task_text="ai task 2", source_quote="q2")], "claude-opus-5")
cmp = c.post("/api/ai-extraction/compare", json={"meeting_id": r["id"]}).get_json()
ok(len(cmp["builtin"]) == 1 and len(cmp["ai"]) == 2 and tasks(r["id"]) == before, "H1 compare returns both, meeting untouched")
c.post(f"/api/ai-extraction/comparisons/{cmp['id']}/verdicts", json={"builtin": [False], "ai": [True, True]})
sb = c.get("/api/ai-extraction/scoreboard").get_json()
ok({e["engine"].split(" ·")[0]: e["precision"] for e in sb["engines"]} == {"Built-in model": 0.0, "Claude (Anthropic API)": 1.0}, f"H2 scoreboard {sb['engines']}")

# I. parsing robustness
ax.run_engine = real_run_engine
items = ax._parse_items('```json\n{"commitments":[{"task_text":"a","owner_name":"","due_date":"Friday","confidence":1.7,"source_quote":""},{"task_text":"","owner_name":"x"},"junk"]}\n```')
ok(items == [{"task_text": "a", "owner_name": "UNRESOLVED", "due_date": None, "confidence_score": 1.0, "source_quote": "a"}], f"I1 parse/clean {items}")
try:
    ax._parse_items("not json"); ok(False, "I2")
except ax.AIExtractionError as e:
    ok(True, f"I2 bad JSON -> {e}")

# J. Anthropic request shape (fake client): Opus 5 -> beta + fallbacks default; Sonnet -> plain
import anthropic
calls = []
class FakeBlock: type, text = "text", '{"commitments": []}'
class FakeResp: stop_reason, model, content = "end_turn", "m", [FakeBlock()]
class FakeMsgs:
    def __init__(self, kind): self.kind = kind
    def create(self, **kw): calls.append((self.kind, kw)); return FakeResp()
class FakeBeta:
    messages = FakeMsgs("beta")
class FakeClient:
    def __init__(self, **kw): self.messages, self.beta = FakeMsgs("plain"), FakeBeta()
orig = anthropic.Anthropic; anthropic.Anthropic = FakeClient
ax._call_anthropic({"api_key": "k", "model": "claude-opus-5"}, "t", "2026-09-24")
ax._call_anthropic({"api_key": "k", "model": "claude-sonnet-5"}, "t", "2026-09-24")
anthropic.Anthropic = orig
(k0, kw0), (k1, kw1) = calls
ok(k0 == "beta" and kw0["fallbacks"] == "default" and kw0["betas"] == ["server-side-fallback-2026-07-01"]
   and kw0["output_config"]["format"]["type"] == "json_schema", "J1 opus-5: beta + fallbacks + structured output")
ok(k1 == "plain" and "fallbacks" not in kw1 and "output_config" in kw1, "J2 sonnet-5: plain call")

# K. real Anthropic API with an invalid key (free: rejected before any tokens)
try:
    ax._call_anthropic({"api_key": "sk-ant-invalid-test-key", "model": "claude-opus-5"}, "t", "2026-09-24"); ok(False, "K")
except ax.AIExtractionError as e:
    ok("rejected the API key" in str(e) or "reach" in str(e), f"K live API, bad key -> {e}")

# L. OpenAI-compatible over real HTTP against a local fake server
seen = {}
class H(BaseHTTPRequestHandler):
    def do_POST(self):
        seen["path"], seen["auth"] = self.path, self.headers.get("Authorization")
        seen["body"] = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        content = json.dumps({"commitments": [{"task_text": "t1", "owner_name": "Ana", "due_date": "2026-10-01", "confidence": 0.8, "source_quote": "q"}]})
        out = json.dumps({"model": "llama-test", "choices": [{"finish_reason": "stop", "message": {"content": content}}]}).encode()
        self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(out)
    def log_message(self, *a): pass
srv = HTTPServer(("127.0.0.1", 0), H); threading.Thread(target=srv.serve_forever, daemon=True).start()
items, model = ax._call_openai_compatible({"base_url": f"http://127.0.0.1:{srv.server_port}/v1", "api_key": "k", "model": "llama"}, "t", "2026-09-24")
ok(seen["path"] == "/v1/chat/completions" and seen["auth"] == "Bearer k" and seen["body"]["response_format"] == {"type": "json_object"}
   and items[0]["owner_name"] == "Ana" and model == "llama-test", f"L openai-compatible HTTP: {items}")
srv.shutdown()
