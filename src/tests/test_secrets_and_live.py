"""Secret encryption at rest and live push updates (0.8.0). Uses a throwaway
encryption key and a temporary database; never touches secret.key or
commitment_tracker.db, and never sends anything over the network."""

import base64, hashlib, hmac, json, os, queue, sqlite3
from _support import ok, fresh_app, signed_post, webhook_setup  # noqa: E402 (also puts the project on sys.path)
import secret_store, live_events
from cryptography.fernet import Fernet

appmod, c, tmp = fresh_app("me@corp.com")
import transcript_parser as tp
tp.extract_commitments = lambda text, meeting_date=None, threshold=0.9: []
tp.extract_commitments_from_turns = lambda turns, meeting_date=None, threshold=0.9: []
raw = lambda sql: sqlite3.connect(tmp).execute(sql).fetchone()[0]

# ---------------- 1. secret_store ----------------
enc = secret_store.encrypt("hunter2")
ok(enc.startswith("enc:v1:") and "hunter2" not in enc, "1a encrypt adds prefix, hides value")
ok(secret_store.decrypt(enc) == "hunter2", "1b round trip")
ok(secret_store.encrypt(enc) == enc, "1c encrypting twice is a no-op")
ok(secret_store.decrypt("plain-legacy") == "plain-legacy", "1d legacy plain text passes through")
ok(secret_store.load_config('{"a": 1}') == {"a": 1} and secret_store.load_config(secret_store.dump_config({"a": 1})) == {"a": 1},
   "1e load_config reads legacy JSON and encrypted JSON")
ok(secret_store.encrypt("") == "" and secret_store.encrypt(None) is None, "1f empty values stay empty")

# ---------------- 2. everything saved is encrypted at rest ----------------
email_cfg = {"smtp_host": "smtp.x.com", "smtp_port": "587", "smtp_security": "starttls", "username": "me",
             "password": "smtp-pass-9876", "from_email": "me@corp.com"}
c.post("/api/nudge-settings", json={"channel": "email", "cadence": "day_before", "config": email_cfg})
c.post("/api/nudge-settings", json={"channel": "slack", "cadence": "day_before", "webhook_or_key": "xoxb-slack-token-5555"})
c.post("/api/tracker-settings", json={"provider": "jira", "config": {"base_url": "https://x.atlassian.net", "project_key": "OPS",
                                                                    "email": "me@corp.com", "api_token": "jira-token-4321"}})
fathom_secret = "whsec_" + base64.b64encode(b"fathom-unit-secret").decode()
c.post("/api/transcript-source-settings", json={"provider": "fathom", "config": {"webhook_secret": fathom_secret}})
c.post("/api/ai-extraction/settings", json={"engine": "anthropic", "config": {"api_key": "sk-ant-aikey-7777"}, "consent": True})

db = sqlite3.connect(tmp)
columns = [("nudge_settings", "webhook_or_key"), ("nudge_settings", "config_json"), ("tracker_settings", "config_json"),
           ("transcript_source_settings", "config_json"), ("ai_extraction_settings", "config_json")]
values = [v for t, col in columns for (v,) in db.execute(f"SELECT {col} FROM {t}")]
dump = " ".join(values)
ok(all(v.startswith("enc:v1:") for v in values) and len(values) == 7, f"2a all {len(values)} saved credential values encrypted")
ok(not any(s in dump for s in ("smtp-pass", "xoxb-slack", "jira-token", "fathom-unit", "sk-ant-aikey", fathom_secret)),
   "2b no secret appears in the raw database")

# ---------------- 3. secrets never go back to the browser ----------------
t = c.get("/api/tracker-settings").get_json()
ok(t["config"]["api_token"] == "" and t["secret_hints"]["api_token"].endswith("4321") and "jira-token" not in json.dumps(t),
   f"3a tracker GET hides api_token, gives hint {t['secret_hints']}")
ok(t["config"]["project_key"] == "OPS", "3b non-secret tracker fields still returned")
s = c.get("/api/transcript-source-settings").get_json()
ok(s["config"]["webhook_secret"] == "" and fathom_secret not in json.dumps(s) and s["secret_hints"].get("webhook_secret"),
   "3c transcript-source GET hides webhook_secret, gives hint")
n = {x["channel"]: x for x in c.get("/api/nudge-settings").get_json()}
ok(n["slack"]["webhook_or_key"].endswith("5555") and "xoxb" not in n["slack"]["webhook_or_key"] and n["email"]["webhook_or_key"] == "me@corp.com",
   "3d nudge list: Slack token masked, email from-address readable")

r = c.post("/api/tracker-settings", json={"provider": "jira", "config": {"base_url": "https://x.atlassian.net", "project_key": "NEW",
                                                                        "email": "me@corp.com", "api_token": ""}})
stored = secret_store.load_config(raw("SELECT config_json FROM tracker_settings"))
ok(r.status_code == 201 and stored["api_token"] == "jira-token-4321" and stored["project_key"] == "NEW", "3e blank secret on save keeps the stored one")
r = c.post("/api/tracker-settings", json={"provider": "custom", "custom_name": "X", "config": {"endpoint": "https://e", "api_key": ""}})
ok(r.status_code == 400, "3f switching provider doesn't carry another provider's secret over")
c.post("/api/transcript-source-settings", json={"provider": "fathom", "config": {"webhook_secret": ""}})
ok(secret_store.load_config(raw("SELECT config_json FROM transcript_source_settings"))["webhook_secret"] == fathom_secret,
   "3g Fathom secret kept on blank re-save")

# ---------------- 4. integrations still work with encrypted values ----------------
token = c.get("/api/transcript-source-settings").get_json()["webhook_token"]
body = json.dumps({"title": "Encrypted Fathom", "transcript": [{"speaker": {"display_name": "Priya"}, "text": "I'll send it."}]})
import time
def fathom_headers(secret=fathom_secret, msg_id="msg_1", ts=None):
    ts = ts or str(int(time.time()))
    key = base64.b64decode(secret.split("_", 1)[1])
    sig = base64.b64encode(hmac.new(key, f"{msg_id}.{ts}.{body}".encode(), hashlib.sha256).digest()).decode()
    return {"webhook-id": msg_id, "webhook-timestamp": ts, "webhook-signature": f"v1,{sig}", "Content-Type": "application/json"}
r = c.post(f"/api/webhooks/fathom/{token}", data=body, headers=fathom_headers())
ok(r.status_code == 201, f"4a Fathom signature verifies against the decrypted secret ({r.status_code})")

delivered = []
real_deliver = appmod.notifications.deliver
appmod.notifications.deliver = lambda ch, key, cfg, commit, reason, **k: delivered.append((ch, key, cfg)) or ("sent", "ok")
setting = sqlite3.connect(tmp); setting.row_factory = sqlite3.Row
slack = setting.execute("SELECT * FROM nudge_settings WHERE channel = 'slack'").fetchone()
mail = setting.execute("SELECT * FROM nudge_settings WHERE channel = 'email'").fetchone()
mid = r.get_json()["meeting_id"]
cid = c.post("/api/commitments", json={"meeting_id": mid, "task_text": "t", "owner_name": "Priya"}).get_json()["id"]
appmod.send_and_log(slack, {"id": cid}, "test"); appmod.send_and_log(mail, {"id": cid}, "test")
ok(delivered[0][1] == "xoxb-slack-token-5555" and delivered[1][2]["password"] == "smtp-pass-9876", "4b nudges get the decrypted token / SMTP password")

# ---------------- 5. migration of a plain-text (pre-0.8.0) database ----------------
backup_mtime_before = appmod.PRE_ENCRYPTION_BACKUP.stat().st_mtime if appmod.PRE_ENCRYPTION_BACKUP.exists() else None
db = sqlite3.connect(tmp)
db.execute("UPDATE nudge_settings SET webhook_or_key = 'xoxb-legacy-1111', config_json = '{}' WHERE channel = 'slack'")
db.execute("UPDATE transcript_source_settings SET config_json = ?", (json.dumps({"webhook_secret": fathom_secret}),))
db.execute("UPDATE ai_extraction_settings SET config_json = ?", (json.dumps({"anthropic": {"api_key": "sk-legacy"}}),)); db.commit()
ok(appmod.encrypt_stored_secrets() == 4, "5a migration encrypts the 4 plain-text values it finds")
ok(raw("SELECT webhook_or_key FROM nudge_settings WHERE channel = 'slack'").startswith("enc:v1:")
   and raw("SELECT config_json FROM ai_extraction_settings").startswith("enc:v1:"), "5b plain-text rows now encrypted")
ok(appmod.encrypt_stored_secrets() == 0, "5c migration is idempotent")
ok(not appmod.PRE_ENCRYPTION_BACKUP.exists() or backup_mtime_before == appmod.PRE_ENCRYPTION_BACKUP.stat().st_mtime,
   "5d test DB never backed up into _snapshots (the real backup, if any, is untouched)")
r = c.post(f"/api/webhooks/fathom/{token}", data=body, headers=fathom_headers(msg_id="msg_2"))
ok(r.status_code == 201, "5e migrated Fathom secret still verifies")
delivered.clear()
appmod.send_and_log(setting.execute("SELECT * FROM nudge_settings WHERE channel = 'slack'").fetchone(), {"id": cid}, "test")
ok(delivered and delivered[0][1] == "xoxb-legacy-1111", "5f migrated Slack token still delivers")
appmod.notifications.deliver = real_deliver

# ---------------- 6. a changed key degrades gracefully ----------------
good_key = os.environ["COMMITMENT_TRACKER_SECRET_KEY"]
os.environ["COMMITMENT_TRACKER_SECRET_KEY"] = Fernet.generate_key().decode(); secret_store.reset()
try:
    secret_store.decrypt(enc); ok(False, "6a")
except secret_store.SecretUnreadable as e:
    ok("re-enter" in str(e), "6a wrong key -> SecretUnreadable with a clear message")
t = c.get("/api/tracker-settings")
ok(t.status_code == 200 and t.get_json()["unreadable"] and t.get_json()["config"] == {}, "6b tracker GET still loads, flags unreadable")
ok(c.get("/api/nudge-settings").status_code == 200, "6c nudge settings list still loads")
r = c.post(f"/api/commitments/{cid}/sync-tracker")
ok(r.status_code == 409 and "re-enter" in r.get_json()["error"], f"6d using an unreadable credential -> 409 with guidance ({r.status_code})")
status, detail = appmod.send_and_log(setting.execute("SELECT * FROM nudge_settings WHERE channel = 'slack'").fetchone(), {"id": cid}, "test")
ok(status == "failed" and "re-enter" in detail, "6e scheduler path logs a failed nudge instead of crashing")
os.environ["COMMITMENT_TRACKER_SECRET_KEY"] = good_key; secret_store.reset()

# ---------------- 7. live events ----------------
def drain(q):
    out = []
    while True:
        try: out.append(q.get_nowait())
        except queue.Empty: return out
mine, other = live_events.subscribe("me@corp.com"), live_events.subscribe("someone@else.com")

paths, wh_secret = webhook_setup(c)
r = signed_post(c, paths["transcript"], {"title": "Live one", "transcript_excerpt": "Priya: I'll do it."}, wh_secret)
ev = drain(mine)
ok(r.status_code == 201 and ev and ev[0]["kind"] == "meeting_created" and ev[0]["data"]["meeting_id"] == r.get_json()["meeting_id"]
   and "job_updated" in [e["kind"] for e in ev], f"7a webhook capture -> meeting_created, then the extraction job's update ({ev})")
c.patch(f"/api/commitments/{cid}", json={"status": "in_progress"})
ok([e["kind"] for e in drain(mine)] == ["commitments_changed"], "7b status change -> commitments_changed")
c.post("/api/meetings", json={"title": ""})
c.patch(f"/api/commitments/{cid}", json={"status": "bogus"})
ok(drain(mine) == [], "7c failed requests publish nothing")
ok(drain(other) == [], "7d another account's subscriber hears nothing")

sqlite3.connect(tmp).execute("UPDATE commitments SET external_ref = 'row-9' WHERE id = ?", (cid,)).connection.commit()
r = signed_post(c, paths["tracker"], {"external_ref": "row-9", "status": "Done", "provider": "google_sheets"}, wh_secret)
ev = drain(mine)
ok(r.status_code == 201 and ev and ev[0]["kind"] == "commitment_status" and ev[0]["data"]["commitment_id"] == cid,
   "7e status change from the sheet -> commitment_status")
appmod.notifications.deliver = lambda *a, **k: ("sent", "ok")
appmod.send_and_log(setting.execute("SELECT * FROM nudge_settings WHERE channel = 'slack'").fetchone(), {"id": cid}, "day_before")
appmod.notifications.deliver = real_deliver
ok([e["kind"] for e in drain(mine)] == ["nudge_sent"], "7f nudge outside a request (scheduler) publishes immediately")

live_events.unsubscribe("me@corp.com", mine); live_events.unsubscribe("someone@else.com", other)
ok(live_events.subscriber_count() == 0, "7g unsubscribe cleans up")
full = live_events.subscribe("me@corp.com")
for _ in range(live_events.MAX_QUEUED + 1):
    live_events.publish("me@corp.com", "x")
ok(live_events.subscriber_count("me@corp.com") == 0, "7h a stuck subscriber is dropped, never blocks the publisher")

# ---------------- 8. /api/events endpoint ----------------
anon = appmod.app.test_client()
ok(anon.get("/api/events").status_code == 401, "8a stream requires a session")
resp = c.get("/api/events", buffered=False)
first = next(resp.response)
ok(resp.status_code == 200 and resp.mimetype == "text/event-stream" and resp.headers["Cache-Control"] == "no-cache",
   "8b text/event-stream, uncached")
first = first.decode() if isinstance(first, bytes) else first
ok("event: ready" in first and live_events.subscriber_count("me@corp.com") == 1, "8c ready frame sent, subscribed")
live_events.publish("me@corp.com", "meeting_created", {"meeting_id": 42})
frame = next(resp.response); frame = frame.decode() if isinstance(frame, bytes) else frame
ok(frame == 'event: meeting_created\ndata: {"meeting_id": 42}\n\n', f"8d event frame format: {frame!r}")
resp.close()
ok(live_events.subscriber_count("me@corp.com") == 0, "8e closing the stream unsubscribes")
