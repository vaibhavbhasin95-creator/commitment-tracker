"""0.9.0: real sign-in (passwords, SSO, CSRF, sessions), background jobs,
signed webhooks, .docx/.pdf upload, clearing meetings/tasks, the meeting
filter, People designations and AI-tailored email drafts.

Temporary database, throwaway keys, fake identity provider / AI engine /
email sender — nothing leaves the machine."""

import io
import json
import os
import time
import zipfile

from _support import fresh_app, login, ok, signed_post, webhook_setup, TEST_PASSWORD

appmod, c, tmp = fresh_app("owner@corp.com")
import auth, jobs, ai_writer, webhook_security, document_text  # noqa: E402
import transcript_parser as tp  # noqa: E402

tp.extract_commitments = lambda text, meeting_date=None, threshold=0.9: (
    [{"task_text": "Send the budget", "owner_name": "Kevin", "due_date": "2026-11-01", "confidence_score": 0.95,
      "source_quote": "Kevin: I'll send the budget."}] if "budget" in (text or "") else [])
tp.extract_commitments_from_turns = lambda turns, meeting_date=None, threshold=0.9: []
new_client = lambda: appmod.app.test_client()

# ---------------- 1. sign-up / sign-in ----------------
anon = new_client()
anon.environ_base["HTTP_X_CT_REQUEST"] = "1"
ok(anon.post("/api/identify", json={"email": "x@y.com"}).status_code == 410, "1a email-only identify is retired (410)")
ok(anon.post("/api/auth/signup", json={"email": "new@corp.com", "password": "short"}).status_code == 400, "1b short password refused")
ok(anon.post("/api/auth/signup", json={"email": "bad", "password": TEST_PASSWORD}).status_code == 400, "1c invalid email refused")
ok(anon.post("/api/auth/signup", json={"email": "owner@corp.com", "password": TEST_PASSWORD}).status_code == 409, "1d existing email can't be re-registered")
ok(anon.get("/api/me").status_code == 401 and anon.get("/api/meetings").status_code == 401, "1e signed-out requests get 401")
ok(anon.get("/dashboard").status_code == 302 and anon.get("/dashboard").headers["Location"].endswith("/"), "1f app pages redirect a signed-out visitor")
r = anon.post("/api/auth/login", json={"email": "owner@corp.com", "password": "wrong-password-1"})
ok(r.status_code == 401 and r.get_json()["error"] == "wrong email or password", "1g wrong password -> generic 401")
r = anon.post("/api/auth/login", json={"email": "nobody@corp.com", "password": "wrong-password-1"})
ok(r.status_code == 401 and r.get_json()["error"] == "wrong email or password", "1h unknown email gets the same message")
import sqlite3  # noqa: E402
raw = sqlite3.connect(tmp)
stored_hash = raw.execute("SELECT password_hash FROM users WHERE email = 'owner@corp.com'").fetchone()[0]
ok(stored_hash and TEST_PASSWORD not in stored_hash and stored_hash.split(":")[0] in ("scrypt", "pbkdf2"), "1i password stored only as a salted hash")

auth.throttle.clear("email:victim@corp.com", "ip:127.0.0.1")
login(new_client(), "victim@corp.com")
attacker = new_client(); attacker.environ_base["HTTP_X_CT_REQUEST"] = "1"
codes = [attacker.post("/api/auth/login", json={"email": "victim@corp.com", "password": f"guess-number-{i}"}).status_code for i in range(6)]
ok(codes[:5] == [401] * 5 and codes[5] == 429, f"1j lockout after {auth.LOGIN_LOCKOUT_FAILURES} failures ({codes})")
r = attacker.post("/api/auth/login", json={"email": "victim@corp.com", "password": TEST_PASSWORD})
ok(r.status_code == 429, "1k even the right password waits out the lockout")
auth.throttle.clear("email:victim@corp.com", "ip:127.0.0.1")

raw.execute("INSERT INTO users (email, created_at) VALUES ('legacy@corp.com', '2026-01-01')"); raw.commit()
r = anon.post("/api/auth/login", json={"email": "legacy@corp.com", "password": "anything-at-all"})
ok(r.status_code == 401 and "manage_users.py set-password" in r.get_json()["error"], "1l pre-0.9.0 account is told how to set a password")
ok(anon.post("/api/auth/signup", json={"email": "legacy@corp.com", "password": TEST_PASSWORD}).status_code == 409,
   "1m a pre-0.9.0 account can't be claimed over the web by signing up")
auth.throttle.clear("email:legacy@corp.com", "ip:127.0.0.1")

# ---------------- 2. sessions + CSRF ----------------
me = c.get("/api/me").get_json()
ok(me["email"] == "owner@corp.com" and me["has_password"] is True, "2a /api/me reports the account")
second = login(new_client(), "owner@corp.com")
ok(second.get("/api/meetings").status_code == 200, "2b second browser signed in")
r = c.post("/api/auth/password", json={"current_password": "nope-nope-nope", "new_password": "another-pass-456"})
ok(r.status_code == 400, "2c password change needs the current password")
r = c.post("/api/auth/password", json={"current_password": TEST_PASSWORD, "new_password": "another-pass-456"})
ok(r.status_code == 200 and c.get("/api/meetings").status_code == 200, "2d password changed; this browser stays signed in")
ok(second.get("/api/meetings").status_code == 401, "2e every other session is signed out by the change")
no_header = new_client()
no_header.post("/api/auth/login", json={"email": "owner@corp.com", "password": "another-pass-456"}, headers={"X-CT-Request": "1"})
r = no_header.post("/api/meetings", json={"title": "forged"})
ok(r.status_code == 403, "2f a state-changing call without the X-CT-Request header is refused (CSRF)")
ok(no_header.get("/api/meetings").status_code == 200, "2g reads don't need the header")
cookie = c.get("/api/me").headers
login_resp = new_client().post("/api/auth/login", json={"email": "owner@corp.com", "password": "another-pass-456"},
                               headers={"X-CT-Request": "1"})
set_cookie = login_resp.headers.get("Set-Cookie", "")
ok("HttpOnly" in set_cookie and "SameSite=Lax" in set_cookie, f"2h session cookie is HttpOnly + SameSite=Lax")
h = c.get("/api/meetings").headers
ok(h["Cache-Control"] == "no-cache" and h["X-Content-Type-Options"] == "nosniff" and h["X-Frame-Options"] == "DENY",
   "2i no-cache + security headers on every response")
other = new_client(); other.environ_base["HTTP_X_CT_REQUEST"] = "1"
other.post("/api/auth/login", json={"email": "owner@corp.com", "password": "another-pass-456"})
ok(c.post("/api/auth/logout-everywhere").status_code == 200 and other.get("/api/me").status_code == 401,
   "2j sign out everywhere ends all sessions")
c = login(new_client(), "owner@corp.com", "another-pass-456")

# ---------------- 3. SSO (OpenID Connect) ----------------
os.environ["CT_SSO_PROVIDERS"] = "google"
os.environ["CT_SSO_GOOGLE_CLIENT_ID"] = "client-123"
os.environ["CT_SSO_GOOGLE_CLIENT_SECRET"] = "shh"
cfg = c.get("/api/auth/config").get_json()
ok(cfg["sso"] == [{"id": "google", "label": "Google"}], f"3a configured provider listed ({cfg['sso']})")
provider = auth.load_sso_providers()["google"]
now = time.time()
good = {"iss": "https://accounts.google.com", "aud": "client-123", "exp": now + 300, "nonce": "n1",
        "email": "SSO.User@Corp.com", "email_verified": True, "sub": "g-1"}
ok(auth.validate_id_token(good, provider, "n1") == "sso.user@corp.com", "3b valid ID token -> lower-cased email")
for label, bad in [("issuer", {"iss": "https://evil.example"}), ("audience", {"aud": "someone-else"}),
                   ("expiry", {"exp": now - 3600}), ("nonce", {"nonce": "other"}), ("unverified email", {"email_verified": False})]:
    try:
        auth.validate_id_token({**good, **bad}, provider, "n1"); ok(False, f"3c rejects bad {label}")
    except auth.AuthError:
        ok(True, f"3c rejects bad {label}")
try:
    auth.validate_id_token({**good, "email": "x@other.com"}, {**provider, "allowed_domains": ["corp.com"]}, "n1"); ok(False, "3d")
except auth.AuthError:
    ok(True, "3d allowed_domains enforced")
ms = {"client_id": "c", "issuer": "https://login.microsoftonline.com/organizations/v2.0",
      "issuer_template": "https://login.microsoftonline.com/{tenant}/v2.0", "tenant": "organizations"}
ok(auth.validate_id_token({**good, "aud": "c", "tid": "t-9", "iss": "https://login.microsoftonline.com/t-9/v2.0"}, ms, "n1"),
   "3e Microsoft multi-tenant issuer checked against the token's tenant")

calls = []
def fake_http(url, data=None):
    calls.append(url)
    if url.endswith("/.well-known/openid-configuration"):
        return {"authorization_endpoint": "https://idp.test/auth", "token_endpoint": "https://idp.test/token"}
    form = dict(x.split("=", 1) for x in data.decode().split("&"))
    claims = {**good, "nonce": pending_nonce[0], "email": "brand.new@corp.com", "sub": "g-77"}
    seg = lambda d: __import__("base64").urlsafe_b64encode(json.dumps(d).encode()).decode().rstrip("=")
    assert form["code_verifier"] and form["code"] == "abc"
    return {"id_token": f"{seg({'alg': 'RS256'})}.{seg(claims)}.sig"}
auth._http_json = fake_http
auth._discovery_cache.clear()
sso = new_client()
r = sso.get("/auth/sso/google/start")
loc = r.headers["Location"]
from urllib.parse import parse_qs, urlparse  # noqa: E402
q = parse_qs(urlparse(loc).query)
pending_nonce = [q["nonce"][0]]
ok(loc.startswith("https://idp.test/auth") and q["code_challenge_method"] == ["S256"] and q["state"], "3f start redirects with state, nonce and PKCE")
ok(sso.get("/auth/sso/google/callback?code=abc&state=WRONG").headers["Location"].startswith("/?error="), "3g wrong state refused")
sso.get("/auth/sso/google/start"); q = parse_qs(urlparse(sso.get("/auth/sso/google/start").headers["Location"]).query)
pending_nonce = [q["nonce"][0]]
r = sso.get(f"/auth/sso/google/callback?code=abc&state={q['state'][0]}")
ok(r.headers["Location"] == "/dashboard" and sso.get("/api/me").get_json()["email"] == "brand.new@corp.com",
   "3h callback signs the user in (new account created)")
ok(sso.get("/api/me").get_json()["sso_identities"] == ["google"], "3i identity linked to the account")
for k in ("CT_SSO_PROVIDERS", "CT_SSO_GOOGLE_CLIENT_ID", "CT_SSO_GOOGLE_CLIENT_SECRET"):
    os.environ.pop(k)

# ---------------- 4. uploads: .docx / .pdf / unsupported ----------------
def make_docx(paragraphs, table=None):
    w = 'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
    body = "".join(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>" for p in paragraphs)
    if table:
        rows = "".join("<w:tr>" + "".join(f"<w:tc><w:p><w:r><w:t>{cell}</w:t></w:r></w:p></w:tc>" for cell in row) + "</w:tr>" for row in table)
        body += f"<w:tbl>{rows}</w:tbl>"
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", f'<?xml version="1.0"?><w:document {w}><w:body>{body}</w:body></w:document>')
    return buf.getvalue()

docx = make_docx(["Weekly sync"], table=[["Kevin", "I'll send the budget by Friday."], ["Priya", "Thanks."]])
r = c.post("/api/meetings", data={"title": "Docx meeting", "meeting_date": "2026-10-15",
                                   "file": (io.BytesIO(docx), "notes.docx")}, content_type="multipart/form-data")
body = r.get_json()
ok(r.status_code == 201 and body["file_kind"] == "docx" and body["commitments_extracted"] == 1, f"4a .docx upload extracted ({body})")
saved = c.get(f"/api/meetings/{body['id']}").get_json()["transcript_excerpt"]
ok("Kevin: I'll send the budget by Friday." in saved, "4b two-column Word table becomes 'Speaker: words'")
docx_meeting = body["id"]

from pypdf import PdfWriter  # noqa: E402
blank = io.BytesIO(); w = PdfWriter(); w.add_blank_page(200, 200); w.write(blank)
r = c.post("/api/meetings", data={"title": "Scan", "file": (io.BytesIO(blank.getvalue()), "scan.pdf")}, content_type="multipart/form-data")
ok(r.status_code == 400 and "scanned" in r.get_json()["error"], "4c scanned PDF (no text layer) explained")
real_pdf = os.path.join(appmod.BASE_DIR, "docs", "Commitment_Tracker_PRD_v0.8.0.pdf")
if os.path.exists(real_pdf):
    text, kind = document_text.extract_text("x.pdf", open(real_pdf, "rb").read())
    ok(kind == "pdf" and "Commitment Tracker" in text and len(text) > 1000, "4d text PDF read with pypdf")
r = c.post("/api/meetings", data={"title": "Old", "file": (io.BytesIO(b"\xd0\xcf\x11\xe0"), "old.doc")}, content_type="multipart/form-data")
ok(r.status_code == 400 and ".docx" in r.get_json()["error"], "4e legacy .doc gets 'save as .docx'")
r = c.post("/api/meetings", data={"title": "Fake", "file": (io.BytesIO(b"hello"), "fake.pdf")}, content_type="multipart/form-data")
ok(r.status_code == 400, "4f a .txt renamed .pdf is rejected")
r = c.post("/api/meetings", data={"title": "Txt", "file": (io.BytesIO("Kevin: I’ll send the budget.".encode("cp1252")), "a.txt")},
           content_type="multipart/form-data")
ok(r.status_code == 201 and r.get_json()["commitments_extracted"] == 1, "4g .txt in Windows-1252 decoded")

# ---------------- 5. meeting filter + clearing ----------------
m2 = c.post("/api/meetings", json={"title": "Second", "transcript_excerpt": "Kevin: I'll send the budget."}).get_json()["id"]
c.post("/api/commitments", json={"meeting_id": m2, "task_text": "extra", "owner_name": "Priya"})
only = c.get(f"/api/commitments?meeting_id={m2}").get_json()
ok(len(only) == 2 and all(x["meeting_id"] == m2 for x in only), "5a ?meeting_id shows only that meeting's tasks")
meetings = {m["id"]: m for m in c.get("/api/meetings").get_json()}
ok(meetings[m2]["commitment_count"] == 2 and meetings[m2]["extraction_status"] == "succeeded", "5b meetings list carries task count + extraction state")
intruder = login(new_client(), "intruder@corp.com")
ok(intruder.delete(f"/api/meetings/{m2}").status_code == 404 and intruder.post("/api/commitments/clear", json={"confirm": True, "meeting_id": m2}).status_code == 404,
   "5c another account can't delete or clear them")
ok(c.post("/api/commitments/clear", json={"meeting_id": m2}).status_code == 400, "5d clearing needs confirm: true")
cid = only[0]["id"]
raw.execute("INSERT INTO nudge_log (commitment_id, triggered_at, channel, status) VALUES (?, 'x', 'email', 'sent')", (cid,)); raw.commit()
r = c.post("/api/commitments/clear", json={"confirm": True, "meeting_id": m2}).get_json()
ok(r["commitments_deleted"] == 2 and c.get(f"/api/commitments?meeting_id={m2}").get_json() == [], "5e one meeting's tasks cleared")
ok(raw.execute("SELECT COUNT(*) FROM nudge_log WHERE commitment_id = ?", (cid,)).fetchone()[0] == 0, "5f their nudge history went too")
ok(c.get(f"/api/meetings/{m2}").status_code == 200 and len(c.get("/api/commitments").get_json()) > 0, "5g the meeting and other tasks stay")
ok(c.delete(f"/api/meetings/{m2}").get_json()["meetings_deleted"] == 1 and c.get(f"/api/meetings/{m2}").status_code == 404, "5h single meeting deleted")
r = c.post("/api/meetings/clear", json={"confirm": True}).get_json()
ok(r["meetings_deleted"] >= 2 and c.get("/api/meetings").get_json() == [] and c.get("/api/commitments").get_json() == [],
   f"5i clear all meetings removes them and their tasks ({r})")

# ---------------- 6. background jobs (worker mode) ----------------
os.environ["CT_JOBS_INLINE"] = "0"
r = c.post("/api/meetings", json={"title": "Async", "transcript_excerpt": "Kevin: I'll send the budget."})
body = r.get_json()
ok(r.status_code == 201 and body["extraction"] == "queued" and body["commitments_extracted"] is None, "6a upload returns at once, extraction queued")
mid = body["id"]
ok(c.get("/api/meetings").get_json()[0]["extraction_status"] == "queued", "6b dashboard can show 'extracting…'")
ok(jobs.run_pending() == 1 and len(c.get(f"/api/commitments?meeting_id={mid}").get_json()) == 1, "6c worker runs the job -> task appears")
job = c.get("/api/jobs").get_json()["jobs"][0]
ok(job["status"] == "succeeded" and job["result"]["commitments_extracted"] == 1 and job["label"] == "Extract commitments", "6d job record + result")

attempts = []
@jobs.handler("flaky_test", "Flaky")
def flaky(payload, job):
    attempts.append(job["attempts"])
    if len(attempts) < 3:
        raise RuntimeError("temporary outage")
    return {"done": True}
db = appmod.get_db(); jid = jobs.enqueue(db, "owner@corp.com", "flaky_test", max_attempts=3); db.commit(); db.close()
jobs.run_pending()
j = jobs.get_job(jid)
ok(j["status"] == "queued" and "retrying in 30s" in j["last_error"], f"6e failure -> re-queued with backoff ({j['last_error']})")
raw.execute("UPDATE jobs SET run_after = '2000-01-01' WHERE id = ?", (jid,)); raw.commit(); jobs.run_pending()
ok("retrying in 60s" in jobs.get_job(jid)["last_error"], "6f backoff doubles")
raw.execute("UPDATE jobs SET run_after = '2000-01-01' WHERE id = ?", (jid,)); raw.commit(); jobs.run_pending()
ok(jobs.get_job(jid)["status"] == "succeeded" and attempts == [1, 2, 3], "6g third attempt succeeds")

@jobs.handler("doomed_test", "Doomed")
def doomed(payload, job):
    raise jobs.PermanentError("can't ever work")
db = appmod.get_db(); jid = jobs.enqueue(db, "owner@corp.com", "doomed_test"); db.commit(); db.close()
jobs.run_pending()
ok(jobs.get_job(jid)["status"] == "failed" and jobs.get_job(jid)["attempts"] == 1, "6h PermanentError fails at once, no retries")
ok(c.post(f"/api/jobs/{jid}/retry").status_code == 200 and jobs.get_job(jid)["status"] == "queued", "6i failed job can be retried from the Jobs page")
ok(c.post(f"/api/jobs/{jid}/cancel").status_code == 200 and jobs.get_job(jid)["status"] == "cancelled", "6j queued job can be cancelled")
ok(intruder.post(f"/api/jobs/{jid}/retry").status_code == 409, "6k another account can't touch it")
raw.execute("UPDATE jobs SET status = 'running' WHERE id = ?", (jid,)); raw.commit()
ok(jobs.recover_stale() == 1 and jobs.get_job(jid)["status"] == "queued", "6l a job left 'running' by a crash is re-queued on start")
raw.execute("UPDATE jobs SET status = 'cancelled' WHERE id = ?", (jid,)); raw.commit()
m_del = c.post("/api/meetings", json={"title": "Doomed meeting", "transcript_excerpt": "budget"}).get_json()["id"]
c.delete(f"/api/meetings/{m_del}")
ok(raw.execute("SELECT status FROM jobs WHERE meeting_id = ?", (m_del,)).fetchone()[0] == "cancelled", "6m deleting a meeting cancels its queued extraction")
appmod.nudge_preferences  # noqa: B018
raw.execute("INSERT OR REPLACE INTO nudge_preferences (user_email, auto_enabled) VALUES ('owner@corp.com', 1)"); raw.commit()
ok(appmod.queue_due_nudge_runs() == 1 and appmod.queue_due_nudge_runs() == 0, "6n periodic nudge check queues one run per account, no pile-up")
jobs.run_pending()
os.environ["CT_JOBS_INLINE"] = "1"

# ---------------- 7. signed webhooks ----------------
paths, secret = webhook_setup(c)
ok(secret.startswith("ctwhsec_") and "ctwhsec_" not in json.dumps(c.get("/api/webhook-security").get_json()), "7a signing secret only shown on reveal")
payload = {"title": "Signed capture", "transcript_excerpt": "Kevin: I'll send the budget."}
ok(c.post(paths["transcript"], json=payload).status_code == 401, "7b unsigned capture refused")
ok(signed_post(c, paths["transcript"], payload, "ctwhsec_wrong").status_code == 401, "7c wrong secret refused")
ok(signed_post(c, paths["transcript"], payload, secret, timestamp=time.time() - 3600).status_code == 401, "7d hour-old timestamp refused")
r = signed_post(c, paths["transcript"], payload, secret, delivery="d-1")
ok(r.status_code == 201 and r.get_json()["commitments_extracted"] == 1, "7e correctly signed capture accepted + extracted")
ok(signed_post(c, paths["transcript"], payload, secret, delivery="d-1").status_code == 401, "7f same delivery id again = replay, refused")
ts = int(time.time()); body = json.dumps(payload)
hdr = {"X-CT-Timestamp": str(ts), "X-CT-Signature": webhook_security.sign(secret, ts, body)}
ok(c.post(paths["transcript"], data=body, headers=hdr, content_type="application/json").status_code == 201
   and c.post(paths["transcript"], data=body, headers=hdr, content_type="application/json").status_code == 401,
   "7g without a delivery id, an identical re-send is still caught")
tampered = json.dumps({**payload, "title": "Tampered"})
ok(c.post(paths["transcript"], data=tampered, headers={**hdr, "X-CT-Timestamp": str(ts + 1)}, content_type="application/json").status_code == 401,
   "7h body changed after signing -> refused")
deliveries = c.get("/api/webhook-security").get_json()["deliveries"]
ok(any(d["status"] == "accepted" for d in deliveries) and any("replay" in (d["detail"] or "") for d in deliveries), "7i deliveries logged for the Integrations page")
new_secret = c.post("/api/webhook-security/rotate").get_json()["secret"]
ok(signed_post(c, paths["transcript"], payload, secret).status_code == 401 and signed_post(c, paths["transcript"], payload, new_secret).status_code == 201,
   "7j rotating the secret retires the old one immediately")
ok(c.post("/api/webhooks/tracker-update", json={"email": "owner@corp.com", "external_ref": "row-1", "status": "done"}).status_code == 410,
   "7k the old unsigned tracker URL is retired (410)")
os.environ["CT_ALLOW_UNSIGNED_TRACKER_WEBHOOK"] = "1"
ok(c.post("/api/webhooks/tracker-update", json={"email": "owner@corp.com", "external_ref": "nope", "status": "done"}).status_code == 404,
   "7l ...unless explicitly re-enabled as a temporary bridge")
os.environ.pop("CT_ALLOW_UNSIGNED_TRACKER_WEBHOOK")
webhook_security.limiter.limit = 3
codes = [c.post(paths["tracker"], json={}).status_code for _ in range(4)]
ok(codes[-1] == 429, f"7m rate limit per webhook URL ({codes})")
webhook_security.limiter.limit = webhook_security.RATE_LIMIT

# ---------------- 8. People designation ----------------
c.post("/api/people", json={"first_name": "Kevin", "last_name": "Heys", "email": "kevin@corp.com", "designation": "VP Finance"})
person = c.get("/api/people").get_json()["people"][0]
ok(person["designation"] == "VP Finance", "8a designation saved")
c.patch(f"/api/people/{person['id']}", json={"first_name": "Kevin", "last_name": "Heys", "email": "kevin@corp.com", "designation": "CFO"})
ok(c.get("/api/people").get_json()["people"][0]["designation"] == "CFO", "8b designation edited")
c.post("/api/people/upload", json={"csv": "Email ID,First Name,Last Name\nkevin@corp.com,Kevin,Heys\n", "mode": "merge"})
ok(c.get("/api/people").get_json()["people"][0]["designation"] == "CFO", "8c a CSV without a Designation column keeps the saved one")
c.post("/api/people/upload", json={"csv": "Email ID,First Name,Last Name,Designation\nkevin@corp.com,Kevin,Heys,Chief Financial Officer\n", "mode": "merge"})
ok(c.get("/api/people").get_json()["people"][0]["designation"] == "Chief Financial Officer", "8d a CSV with designations updates them")
ok("Designation" in c.get("/api/people/template.csv").get_data(as_text=True), "8e template CSV has a Designation column")

# ---------------- 9. email drafts + AI tailoring ----------------
task = {"task_text": "Send the budget", "owner_name": "Kevin Heys", "owner_email": "kevin@corp.com", "due_date": "2026-11-01",
        "status": "pending", "meeting_title": "FY27 planning"}
values = ai_writer.context_for(task, "overdue", "CFO", "Asha")
ok(ai_writer.fill("Hi {owner_first_name}, {task} was due {due_date}. {unknown} {", values) == "Hi Kevin, Send the budget was due 2026-11-01. {unknown} {",
   "9a placeholders filled; unknown braces left alone")
cfg = {"draft_subject": "{reason}: {task}", "draft_body": "Hi {owner_first_name},\nplease send it.\n{sender_name}", "ai_mode": "off"}
db = appmod.get_db()
w = ai_writer.personalise(db, "owner@corp.com", cfg, task, "overdue", "CFO", "Asha")
ok(w["ai_used"] is False and w["subject"] == "Overdue: Send the budget" and w["body"].endswith("Asha"), "9b AI off -> your draft, filled in")
w = ai_writer.personalise(db, "owner@corp.com", {**cfg, "ai_mode": "extraction_engine"}, task, "overdue", "CFO", "Asha")
ok(w["ai_used"] is False and "AI extraction page" in w["note"] and w["subject"] == "Overdue: Send the budget",
   "9c AI on but no engine saved -> draft sent as written, with the reason")
seen = {}
def fake_claude(config, message):
    seen["message"] = message
    return json.dumps({"subject": "Budget — overdue", "body": "Kevin, a brief note: the budget (due 2026-11-01) is overdue.\nAsha",
                       "tone": "brief, executive"}), "claude-test"
ai_writer._call_anthropic = fake_claude
w = ai_writer.personalise(db, "owner@corp.com", {**cfg, "ai_mode": "extraction_engine"}, task, "overdue", "CFO", "Asha",
                          engine_override=("anthropic", {"api_key": "k"}))
ok(w["ai_used"] and w["subject"] == "Budget — overdue" and "CFO" in w["note"] and "Designation: CFO" in seen["message"]
   and "please send it." in seen["message"], "9d AI rewrite gets the draft + designation and its text is used")
ai_writer._call_anthropic = lambda config, message: ("not json", "m")
w = ai_writer.personalise(db, "owner@corp.com", {**cfg, "ai_mode": "extraction_engine"}, task, "overdue", "CFO", "Asha",
                          engine_override=("anthropic", {"api_key": "k"}))
ok(not w["ai_used"] and "valid JSON" in w["note"], "9e a bad AI reply falls back to the draft")
db.close()

c.post("/api/ai-extraction/settings", json={"engine": "anthropic", "config": {"api_key": "sk-ant-x", "model": "claude-test"}, "consent": True})
ai_writer._call_anthropic = fake_claude
r = c.post("/api/email-draft/preview", json={"config": {**cfg, "ai_mode": "extraction_engine"}, "designation": "Intern"}).get_json()
ok(r["ai_used"] and r["designation"] == "Intern" and "Designation: Intern" in seen["message"], "9f preview uses the AI extraction engine")
email_cfg = {"smtp_host": "smtp.x", "smtp_port": "587", "smtp_security": "starttls", "username": "u", "password": "p",
             "from_email": "owner@corp.com", "draft_subject": "{reason}: {task}", "draft_body": "Hi {owner_first_name}", "ai_mode": "extraction_engine",
             "sender_name": "Asha"}
ok(c.post("/api/nudge-settings", json={"channel": "email", "cadence": "on_due", "config": email_cfg}).status_code == 201, "9g email channel saves the draft")
saved = {s["channel"]: s for s in c.get("/api/nudge-settings").get_json()}["email"]["config"]
ok(saved["draft_body"] == "Hi {owner_first_name}" and saved["ai_mode"] == "extraction_engine" and "password" not in saved, "9h draft returned, password not")
mid = c.post("/api/meetings", json={"title": "Nudge me", "transcript_excerpt": "x"}).get_json()["id"]
cid = c.post("/api/commitments", json={"meeting_id": mid, "task_text": "Send the budget", "owner_name": "Kevin Heys",
                                      "due_date": "2026-11-01"}).get_json()["id"]
sent = []
real_deliver = appmod.notifications.deliver
appmod.notifications.deliver = lambda ch, key, conf, commit, reason, message=None: sent.append(message) or ("sent", f"emailed {commit['owner_email']}")
r = c.post(f"/api/commitments/{cid}/send-test-nudge", json={"channel": "email"}).get_json()
appmod.notifications.deliver = real_deliver
ok(sent and sent[0][0] == "Budget — overdue" and "AI-tailored for Chief Financial Officer" in r["detail"],
   f"9i a real nudge to a CFO is AI-tailored using their People designation ({r['detail']})")
