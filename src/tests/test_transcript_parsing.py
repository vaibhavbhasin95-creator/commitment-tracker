"""Transcript parsing and input-validation edge cases (0.2.x / 0.7.0), plus the
Fathom webhook signature check. Fast: no ML model is loaded."""

import base64
import hashlib
import hmac
import json

from _support import fresh_app, ok

import app as appmod
import transcript_parser as tp


def turns(text):
    return [(r["speaker"], r["text"]) for r in tp.to_turns(text).to_dict("records")]


# --- speaker-turn formats -------------------------------------------------
ok(turns("Priya: I will do it.\nKarthik: Thanks.") == [("Priya", "I will do it."), ("Karthik", "Thanks.")], "inline 'Name: text'")
ok(turns("[10:00 AM] Bhuvan:\nLet's start.\n[10:02 AM] Abhinav Reddy:\nQuestion.") == [("Bhuvan", "Let's start."), ("Abhinav Reddy", "Question.")],
   "'[10:00 AM] Name:' header lines (FY2027 format)")
ok(turns("[09:00] Maya Singh: Good morning.\n[09:02] Arjun Mehta: Hi.") == [("Maya Singh", "Good morning."), ("Arjun Mehta", "Hi.")],
   "'[09:00] Name: text' on one line (0.7.0 fix)")
ok(turns("WEBVTT\n\n1\n00:12:31.020 --> 00:12:31.710\nRizwana: Okay sure.") == [("Rizwana", "Okay sure.")], "WebVTT cue scaffolding skipped")
ok(turns("12:34 - 12\nI will send it.") == [("UNKNOWN", "I will send it.")], "numeric 'speaker' becomes UNKNOWN")
ok(turns("Meeting ID: X-1\nFormat: Virtual\nDate: today\nPriya: Hello there.") == [("Priya", "Hello there.")], "preamble fields are not speakers")
got = turns("Priya: Wrap up.\nACTION ITEMS\n1. Kevin - do X")
ok(got[-1] == ("UNKNOWN", "1. Kevin - do X"), "lines after a section heading are not credited to the last speaker")
ok(turns("") == [] and turns("   \n\n") == [], "empty transcript -> no turns")
ok(tp.SELF.search(tp.normalize_text("I’ll send it")) is not None, "curly apostrophe normalised before owner detection")

# --- due-date resolution (meeting on Monday 2026-09-21) --------------------
for phrase, expected in [("Thursday", "2026-09-24"), ("tomorrow", "2026-09-22"), ("eod", "2026-09-21"), ("this week", "2026-09-25"),
                         ("next week", "2026-09-28"), ("Monday", "2026-09-28"), ("someday", None), (None, None)]:
    ok(tp.resolve_due_date(phrase, "2026-09-21") == expected, f"due phrase {phrase!r} -> {expected}")

# --- binary-upload guard -----------------------------------------------------
ok(appmod.looks_like_binary_text("PK\x03\x04garbage"), "ZIP/.docx bytes detected")
ok(appmod.looks_like_binary_text("ab�" * 50), "replacement-character noise detected")
ok(not appmod.looks_like_binary_text("Priya: I'll send it by Friday."), "normal text accepted")
_, client, _ = fresh_app()
r = client.post("/api/meetings", json={"title": "x", "transcript_excerpt": "PK\x03\x04\x14\x00binary"})
ok(r.status_code == 400, "upload API rejects binary content")
ok(client.post("/api/meetings", json={"title": "", "transcript_excerpt": "a"}).status_code == 400, "upload API requires a title")

# --- Fathom webhook signature (HMAC-SHA256, Svix style) --------------------
secret = "whsec_" + base64.b64encode(b"unit-test-secret").decode()
body = json.dumps({"title": "T"}).encode()


def signed_headers(sig_secret=secret, msg_id="msg_1", ts="1700000000"):
    key = base64.b64decode(sig_secret.split("_", 1)[1])
    sig = base64.b64encode(hmac.new(key, f"{msg_id}.{ts}.{body.decode()}".encode(), hashlib.sha256).digest()).decode()
    return {"webhook-id": msg_id, "webhook-timestamp": ts, "webhook-signature": f"v1,{sig}"}


with appmod.app.test_request_context(data=body, headers=signed_headers()):
    ok(appmod.verify_fathom_signature(secret, appmod.request), "valid signature accepted")
with appmod.app.test_request_context(data=body, headers=signed_headers(sig_secret="whsec_" + base64.b64encode(b"wrong").decode())):
    ok(not appmod.verify_fathom_signature(secret, appmod.request), "signature from a different secret rejected")
with appmod.app.test_request_context(data=body + b" ", headers=signed_headers()):
    ok(not appmod.verify_fathom_signature(secret, appmod.request), "tampered body rejected")
with appmod.app.test_request_context(data=body, headers={}):
    ok(not appmod.verify_fathom_signature(secret, appmod.request), "missing signature headers rejected")
with appmod.app.test_request_context(data=body, headers=signed_headers()):
    ok(not appmod.verify_fathom_signature("", appmod.request), "empty stored secret never verifies")
r = client.post("/api/webhooks/fathom/not-a-real-token", data=body, headers=signed_headers())
ok(r.status_code == 404, "unknown webhook token -> 404")
