"""People directory, owner tagging, real nudges, schedule and DB migration (0.6.0).
Uses a fake SMTP server and local webhook receivers - never sends real messages."""

import sys, json, sqlite3, tempfile, threading, shutil, smtplib
from datetime import date, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from _support import login, ROOT, ok  # noqa: E402 (also puts the project on sys.path)
import app as appmod, people_directory as pd, notifications as nt, transcript_parser as tp


# ---------------- 1. CSV parsing ----------------
people, errors = pd.parse_csv("\ufeffFirst Name,Last Name,Email ID,Corporate User Name\n"
                              "Kevin,Heys,Kevin.Heys@corp.com,kheys\nKevin,Brown,kevin.b@corp.com,CORP\\kbrown\n"
                              "Bhuvan,,bhuvan@corp.com,\nBad,Row,not-an-email,x\nDup,Row,kevin.b@corp.com,y\n,,,\n"
                              "Abhinav,Reddy,abhinav@corp.com,areddy\nSarah Jane,Xian,sarah@corp.com,sxian\n")
ok(len(people) == 5 and people[0]["email"] == "kevin.heys@corp.com", f"1a parsed {len(people)} people, emails lowercased")
ok(len(errors) == 2 and "not-an-email" in errors[0] and "more than once" in errors[1], f"1b row errors reported: {errors}")
p2, _ = pd.parse_csv("email,firstname,surname,username\nana@x.io,Ana,Lima,alima\n")
ok(p2 == [{"first_name": "Ana", "last_name": "Lima", "email": "ana@x.io", "username": "alima", "designation": ""}], "1c alternate header spellings")
p3, _ = pd.parse_csv("Email ID,First Name,Job Title\nvp@x.io,Vee,VP Finance\n")
ok(p3[0]["designation"] == "VP Finance", "1d a 'Job Title' column is read as designation")
try:
    pd.parse_csv("Name,Phone\nA,1\n"); ok(False, "1d")
except ValueError as e:
    ok("email column" in str(e), f"1d missing email column -> {e}")

# ---------------- 2. matching ----------------
cases = {
    "Kevin Heys": ("kevin.heys@corp.com", "auto"), "kevin heys": ("kevin.heys@corp.com", "auto"),
    "Kevin": (None, "ambiguous"), "Bhuvan": ("bhuvan@corp.com", "auto"), "Abhinav Reddy": ("abhinav@corp.com", "auto"),
    "kheys": ("kevin.heys@corp.com", "auto"), "kbrown": ("kevin.b@corp.com", "auto"), "Heys": ("kevin.heys@corp.com", "auto"),
    "Sarah Xian": ("sarah@corp.com", "auto"), "Sarah": ("sarah@corp.com", "auto"), "UNRESOLVED": (None, "none"),
    "UNKNOWN": (None, "none"), "Francoise Tesson": (None, "none"),
}
bad = {n: pd.match_owner(n, people)[:2] for n, want in cases.items() if pd.match_owner(n, people)[:2] != want}
ok(not bad, f"2a {len(cases)} name cases match as expected {bad or ''}")
ok(pd.match_owner("Kevin", people)[2] == ["kevin.b@corp.com", "kevin.heys@corp.com"], "2b ambiguous returns candidates")

# ---------------- 3. app flow ----------------
tmp = Path(tempfile.mkdtemp()) / "t.db"; appmod.DB_PATH = tmp; appmod.init_db()
EXTRACTED = [
    {"task_text": "Model cash flow", "owner_name": "Kevin Heys", "due_date": None, "confidence_score": 0.9, "source_quote": "q1"},
    {"task_text": "Book offsite", "owner_name": "Kevin", "due_date": None, "confidence_score": 0.9, "source_quote": "q2"},
    {"task_text": "Send report", "owner_name": "Francoise Tesson", "due_date": None, "confidence_score": 0.9, "source_quote": "q3"},
]
tp.extract_commitments = lambda t, meeting_date=None, threshold=0.9: [dict(x) for x in EXTRACTED]
appmod.ai_extraction = None  # keep this test on the built-in path
c = appmod.app.test_client(); login(c, "me@corp.com")
csv_text = "First Name,Last Name,Email ID,Corporate User Name\nKevin,Heys,kevin.heys@corp.com,kheys\nKevin,Brown,kevin.b@corp.com,kbrown\n"
r = c.post("/api/people/upload", json={"csv": csv_text}).get_json()
ok(r["added"] == 2 and r["row_errors"] == [], f"3a upload: {r}")
mid = c.post("/api/meetings", json={"title": "m", "transcript_excerpt": "x"}).get_json()["id"]
tasks = {t["task_text"]: t for t in c.get(f"/api/meetings/{mid}").get_json()["commitments"]}
ok(tasks["Model cash flow"]["owner_email"] == "kevin.heys@corp.com" and tasks["Model cash flow"]["owner_match"] == "auto", "3b unique name auto-tagged on creation")
ok(tasks["Book offsite"]["owner_email"] is None and tasks["Book offsite"]["owner_match"] == "ambiguous", "3c shared first name flagged ambiguous, not guessed")
ok(tasks["Send report"]["owner_match"] == "none", "3d unknown person -> none")
detail = c.get(f"/api/commitments/{tasks['Book offsite']['id']}").get_json()
ok(detail["owner_candidates"] == ["kevin.b@corp.com", "kevin.heys@corp.com"], "3e task detail lists candidates for ambiguous owner")

bid = tasks["Book offsite"]["id"]
ok(c.patch(f"/api/commitments/{bid}", json={"owner_email": "kevin.b@corp.com"}).status_code == 200, "3f manual owner email saved")
ok(c.patch(f"/api/commitments/{bid}", json={"owner_email": "nope"}).status_code == 400, "3g invalid manual email rejected")
r = c.patch(f"/api/commitments/{bid}", json={"status": "in_progress"}); ok(r.status_code == 200 and r.get_json() == {"ok": True, "changed": ["status"], "tracker_sync_error": None}, "3h status-only PATCH reports what changed")
ok(c.patch(f"/api/commitments/{bid}", json={"status": "bogus"}).get_json()["error"] == "status must be one of pending, in_progress, done", "3i invalid status: same error as before")
ok(c.patch(f"/api/commitments/{bid}", json={}).status_code == 400, "3j empty PATCH rejected as before")

# adding Francoise re-matches existing tasks, but never overrides the manual pick
r = c.post("/api/people", json={"first_name": "Francoise", "last_name": "Tesson", "email": "ft@corp.com"}).get_json()
tasks = {t["task_text"]: t for t in c.get(f"/api/meetings/{mid}").get_json()["commitments"]}
ok(tasks["Send report"]["owner_email"] == "ft@corp.com" and tasks["Send report"]["owner_match"] == "auto", f"3k new person back-fills existing task {r['tasks_rematched']}")
ok(tasks["Book offsite"]["owner_email"] == "kevin.b@corp.com" and tasks["Book offsite"]["owner_match"] == "manual", "3l manual pick survives re-matching")
man = c.post("/api/commitments", json={"meeting_id": mid, "task_text": "hand task", "owner_name": "Kevin Heys"}).get_json()
ok(c.get(f"/api/commitments/{man['id']}").get_json()["owner_email"] == "kevin.heys@corp.com", "3m hand-added task auto-tagged")
man2 = c.post("/api/commitments", json={"meeting_id": mid, "task_text": "hand task 2", "owner_name": "Kevin", "owner_email": "x@corp.com"}).get_json()
ok(c.get(f"/api/commitments/{man2['id']}").get_json()["owner_match"] == "manual", "3n explicit email on hand-added task counts as manual")
r = c.post("/api/people/upload", json={"csv": "Email ID,First Name\nnew@corp.com,Zed\n", "mode": "replace"}).get_json()
ok(r["removed"] == 3 and len(c.get("/api/people").get_json()["people"]) == 1, f"3o replace mode removes people not in file {r['removed']}")
tasks = {t["task_text"]: t for t in c.get(f"/api/meetings/{mid}").get_json()["commitments"]}
ok(tasks["Model cash flow"]["owner_email"] is None and tasks["Book offsite"]["owner_email"] == "kevin.b@corp.com", "3p removed person un-tags auto matches; manual kept")
c.post("/api/people/upload", json={"csv": csv_text + "Francoise,Tesson,ft@corp.com,\n", "mode": "replace"})

# ---------------- 4. channel delivery ----------------
sent_mail = []
class FakeSMTP:
    def __init__(self, host, port, timeout=None, context=None): self.host, self.port = host, port
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def starttls(self, context=None): self.tls = True
    def login(self, u, p):
        if p != "app-pass": raise smtplib.SMTPAuthenticationError(535, b"bad")
    def send_message(self, msg): sent_mail.append(msg)
smtplib.SMTP = FakeSMTP; smtplib.SMTP_SSL = FakeSMTP

email_cfg = {"smtp_host": "smtp.gmail.com", "smtp_port": "587", "smtp_security": "starttls", "username": "me@gmail.com", "password": "app-pass", "from_email": "me@gmail.com"}
ok(c.post("/api/nudge-settings", json={"channel": "email", "cadence": "day_before", "config": dict(email_cfg, smtp_host="")}).status_code == 400, "4a email needs SMTP host")
ok(c.post("/api/nudge-settings", json={"channel": "email", "cadence": "day_before", "config": email_cfg}).status_code == 201, "4b email channel saved")
listed = c.get("/api/nudge-settings").get_json()[0]
ok("app-pass" not in json.dumps(listed) and listed["has_password"], "4c SMTP password never returned to browser")
c.post("/api/nudge-settings", json={"channel": "email", "cadence": "on_due", "config": dict(email_cfg, password="")})
stored = appmod.secret_store.load_config(sqlite3.connect(tmp).execute("select config_json from nudge_settings where channel='email'").fetchone()[0])
ok(stored["password"] == "app-pass", "4d blank password field keeps saved password")

tid = tasks["Model cash flow"]["id"]  # owner re-matched to kevin.heys by the replace upload above
r = c.post(f"/api/commitments/{tid}/send-test-nudge", json={"channel": "email"}).get_json()
ok(r["status"] == "sent" and sent_mail and sent_mail[-1]["To"] == "kevin.heys@corp.com" and "Model cash flow" in sent_mail[-1]["Subject"], f"4e real email to owner: {r['detail']}")
r = c.post(f"/api/commitments/{tasks['Send report']['id']}/send-test-nudge", json={"channel": "email"}).get_json()
ok(r["status"] == "sent", "4f re-matched owner can be emailed")
nomail = c.post("/api/commitments", json={"meeting_id": mid, "task_text": "orphan", "owner_name": "Nobody"}).get_json()["id"]
r = c.post(f"/api/commitments/{nomail}/send-test-nudge", json={"channel": "email"}).get_json()
ok(r["status"] == "skipped" and "People page" in r["detail"], f"4g no email -> skipped, explained: {r['detail']}")
st, d = nt.send_email(dict(email_cfg, password="wrong"), "a@b.co", "s", "b")
ok(st == "failed" and "app password" in d, f"4h bad SMTP login -> clear error: {d}")
r = c.post("/api/nudge-settings/email/test").get_json()
ok(r["status"] == "sent" and sent_mail[-1]["To"] == "me@corp.com", "4i channel test sends a sample to your own address")
hist = c.get(f"/api/commitments/{tid}").get_json()["nudge_history"]
ok(hist[0]["status"] == "sent" and hist[0]["reason"] == "test" and "kevin.heys" in hist[0]["detail"], "4j nudge history records status/reason/detail")

received = []
class Hook(BaseHTTPRequestHandler):
    def do_POST(self):
        received.append((self.path, json.loads(self.rfile.read(int(self.headers["Content-Length"])))))
        self.send_response(202 if self.path == "/teams" else 200); self.end_headers(); self.wfile.write(b"{}")
    def log_message(self, *a): pass
srv = HTTPServer(("127.0.0.1", 0), Hook); threading.Thread(target=srv.serve_forever, daemon=True).start()
base = f"http://127.0.0.1:{srv.server_port}"
commit = {"id": 1, "task_text": "Model cash flow", "owner_name": "Kevin Heys", "owner_email": "kevin.heys@corp.com", "due_date": "2026-09-28", "status": "pending", "meeting_title": "FY27"}
st, d = nt.send_teams(base.replace("http", "https", 0) if False else base + "/teams", commit, "day_before", "s", "b")
ok(st == "failed", "4k Teams requires https URL")
nt_post = nt._post_json
st, d = nt.deliver("custom", base + "/custom", {}, commit, "on_due")
p = received[-1][1]
ok(st == "sent" and p["recipient_email"] == "kevin.heys@corp.com" and p["reason"] == "on_due" and p["task"] == "Model cash flow", f"4l custom webhook payload: {sorted(p)}")
# Teams payload shape (https required, so call the internals with a patched post)
captured = {}
nt._post_json = lambda url, payload, headers=None: captured.update(payload) or "{}"
st, d = nt.deliver("teams", "https://x.logic.azure.com/workflows/abc", {}, commit, "overdue")
ok(st == "sent" and captured["attachments"][0]["contentType"] == "application/vnd.microsoft.card.adaptive" and captured["recipient_email"] == "kevin.heys@corp.com", "4m Teams: adaptive card + recipient fields")
# Slack bot DM: lookup by email then postMessage
calls = []
class FakeResp:
    def __init__(self, body): self.body = body
    def __enter__(self): return self
    def __exit__(self, *a): pass
    def read(self): return json.dumps(self.body).encode()
import urllib.request as ur
real_urlopen = ur.urlopen
def fake_urlopen(req, timeout=None):
    calls.append(req.full_url)
    return FakeResp({"ok": True, "user": {"id": "U123"}})
ur.urlopen = fake_urlopen
nt._post_json = lambda url, payload, headers=None: calls.append((url, payload["channel"])) or json.dumps({"ok": True})
st, d = nt.deliver("slack", "xoxb-test", {}, commit, "on_due")
ok(st == "sent" and "users.lookupByEmail" in calls[0] and "kevin.heys" in calls[0] and calls[1] == ("https://slack.com/api/chat.postMessage", "U123"), f"4n Slack DM via lookupByEmail: {d}")
st, d = nt.deliver("slack", "xoxb-test", {}, dict(commit, owner_email=None), "on_due")
ok(st == "skipped", "4o Slack DM without owner email -> skipped")
ok(nt.deliver("slack", "garbage", {}, commit, "test")[0] == "failed", "4p Slack rejects unknown token/URL format")
ur.urlopen = real_urlopen; nt._post_json = nt_post
srv.shutdown()

# ---------------- 5. schedule ----------------
db = sqlite3.connect(tmp); db.execute("DELETE FROM nudge_log"); db.commit()
today = date(2026, 9, 27)
mk = lambda task, due, owner: c.post("/api/commitments", json={"meeting_id": mid, "task_text": task, "owner_name": owner, "due_date": due}).get_json()["id"]
t_tomorrow, t_today, t_late = mk("due tomorrow", "2026-09-28", "Kevin Heys"), mk("due today", "2026-09-27", "Kevin Heys"), mk("late", "2026-09-20", "Kevin Heys")
db.execute("UPDATE commitments SET due_date = NULL WHERE id NOT IN (?, ?, ?)", (t_tomorrow, t_today, t_late)); db.commit()
c.post("/api/nudge-settings", json={"channel": "email", "cadence": "day_before", "config": email_cfg})
plan = lambda: [(t["task_text"], r) for _, t, r in appmod.due_nudges(appmod.get_db(), "me@corp.com", today)]
ok(plan() == [("due tomorrow", "day_before")], f"5a day-before cadence picks only tomorrow's task {plan()}")
sent_before = len(sent_mail); res = appmod.run_nudges("me@corp.com", today)
ok(len(res) == 1 and res[0]["status"] == "sent" and len(sent_mail) == sent_before + 1, "5b run sends it")
ok(plan() == [], "5c not sent twice")
c.post("/api/nudge-settings", json={"channel": "email", "cadence": "on_due", "config": email_cfg})
ok(plan() == [("due today", "on_due")], "5d on-due cadence")
c.post("/api/nudge-settings", json={"channel": "email", "cadence": "daily_after_overdue", "config": email_cfg})
ok(plan() == [("late", "overdue")], "5e overdue cadence")
appmod.run_nudges("me@corp.com", today); ok(plan() == [], "5f overdue not repeated the same day")
db.execute("UPDATE nudge_log SET triggered_at = '2026-01-01T00:00:00' WHERE reason = 'overdue'"); db.commit()
ok(plan() == [("late", "overdue")], "5g overdue repeats the next day")
c.patch(f"/api/commitments/{t_late}", json={"status": "done"}); ok(plan() == [], "5h done tasks never nudged")
c.post("/api/nudge-settings", json={"channel": "email", "cadence": "daily_after_overdue", "config": email_cfg, "enabled": False})
c.patch(f"/api/commitments/{t_late}", json={"status": "pending"}); ok(plan() == [], "5i disabled channel sends nothing")
ok(c.get("/api/nudge-preferences").get_json() == {"auto_enabled": False}, "5j automatic nudges OFF by default")
ok(c.post("/api/nudge-preferences", json={"auto_enabled": True}).get_json() == {"auto_enabled": True}, "5k can be switched on")
log = c.get("/api/nudges/log").get_json(); ok([(l["task_text"], l["reason"], l["status"]) for l in log] == [("late", "overdue", "sent"), ("due tomorrow", "day_before", "sent")], f"5l activity log lists exactly the 2 sends, newest first")
c.delete("/api/nudge-settings/email"); ok(c.get("/api/nudge-settings").get_json() == [], "5m channel can be deleted")

# ---------------- 6. migration of an existing 0.5.0 database ----------------
old = Path(tempfile.mkdtemp()) / "old.db"
con = sqlite3.connect(old); con.executescript((ROOT / "tests" / "fixtures" / "schema_v0.5.0.sql").read_text())
con.execute("INSERT INTO meetings (owner_email, title, meeting_date, created_at) VALUES ('a@b.co','m','2026-09-01','x')")
con.execute("INSERT INTO commitments (meeting_id, task_text, owner_name, due_date, created_at) VALUES (1,'t','Kevin','2026-09-02','x')")
con.execute("INSERT INTO nudge_settings (user_email, channel, webhook_or_key) VALUES ('a@b.co','slack','https://hooks.slack.com/x')")
con.execute("INSERT INTO nudge_log (commitment_id, triggered_at, channel, status) VALUES (1,'x','slack','mock_sent')"); con.commit(); con.close()
appmod.DB_PATH = old; appmod.migrate_db()
con = sqlite3.connect(old)
cols = lambda t: {r[1] for r in con.execute(f"PRAGMA table_info({t})")}
ok({"owner_match"} <= cols("commitments") and {"config_json"} <= cols("nudge_settings") and {"reason", "detail"} <= cols("nudge_log")
   and cols("people") and cols("nudge_preferences"), "6a old DB gains new columns and tables")
ok(con.execute("select task_text from commitments").fetchone()[0] == "t" and con.execute("select status from nudge_log").fetchone()[0] == "mock_sent", "6b existing data intact")
appmod.migrate_db(); ok(True, "6c migration safe to run twice")
