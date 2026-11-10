"""RAG page and API routes (rag/routes.py, Step 1.6): sign-in and CSRF guards, input checks, answers,
error mapping, the one-time cloud consent, account isolation and re-indexing. The app's own test client,
a temporary database, and stubs for the embedder and the AI: nothing is downloaded or sent anywhere."""

import json
import os

os.environ["RAG_ENABLED"] = "1"  # before _support / app are imported: other suites keep RAG off

from _support import fresh_app, login, ok  # noqa: E402

import ai_extraction  # noqa: E402
import rag.llm as rag_llm  # noqa: E402
import secret_store  # noqa: E402
from rag.errors import RagError  # noqa: E402
from rag.indexer import delete_index, index_meeting  # noqa: E402
from rag.retriever import STOPWORDS, words_in  # noqa: E402

appmod, c, _ = fresh_app("owner@corp.com")
VOCABULARY = {}
LOCAL = {"openai_compatible": {"base_url": "http://localhost:4891/v1", "model": "Test Model", "api_key": ""}}
CLOUD = {"openai_compatible": {"base_url": "https://api.example.com/v1", "model": "cloud-model", "api_key": "sk-secret-key-123"}}
chat_calls = []
chat_behaviour = {"reply": "Booked for Wednesday [1].", "error": None}


def embed(texts):
    """One axis per distinct word (stop words ignored): texts are similar to the extent they share words."""
    out = []
    for text in texts:
        vector = [0.0] * 512
        for word in words_in(text):
            if word not in STOPWORDS:
                vector[VOCABULARY.setdefault(word, len(VOCABULARY))] += 1.0
        out.append(vector)
    return out


def chat(settings, messages, temperature=0.0, max_tokens=600):
    chat_calls.append({"settings": settings, "messages": messages})
    if chat_behaviour["error"]:
        raise chat_behaviour["error"]
    return chat_behaviour["reply"]


rag_llm.embed, rag_llm.EMBEDDING_MODEL_NAME, rag_llm.chat = embed, "stub", chat


def add_meeting(email, title, transcript):
    d = appmod.get_db()
    meeting_id = appmod.create_meeting_row(d, email, title, "2026-11-01", transcript, "upload")
    d.commit()
    index_meeting(d, meeting_id, embed_fn=embed, model_name="stub")
    d.close()
    return meeting_id


def save_engine(email, configs, consent_at=None):
    d = appmod.get_db()
    ai_extraction.ensure_schema(d)
    d.execute("INSERT OR REPLACE INTO ai_extraction_settings VALUES (?, 'builtin', ?, ?, 't')", (email, secret_store.dump_config(configs), consent_at))
    d.commit()
    d.close()


def post(client, path, body=None, **kw):
    return client.post(path, json=body, **kw) if body is not None else client.post(path, **kw)


ASK = "/api/rag/ask"
QUESTION = {"question": "when is the vendor retest"}
m_owner = add_meeting("owner@corp.com", "Vendor review", "Person A: Book the vendor retest for Wednesday.\nPerson B: Yes, the retest is booked. OWNER-ONLY-TEXT")
save_engine("owner@corp.com", LOCAL)  # extraction stays on Built-in: RAG uses the saved local engine anyway

# --- the page and the status route -------------------------------------------------------------
r = c.get("/api/rag/status")
ok(r.status_code == 200 and r.get_json() == {"enabled": True}, "the status route says the feature is on (the sidebar uses it)")
page = c.get("/ask")
ok(page.status_code == 200 and b"Ask your meetings" in page.data, "the Ask page is served to a signed-in user")
ok(b"/api/rag/ask" in page.data and b"/api/rag/consent" in page.data, "...and talks to the ask and consent routes")
anon = appmod.app.test_client()
anon.environ_base["HTTP_X_CT_REQUEST"] = "1"
ok(anon.get("/ask").status_code == 302 and anon.get("/ask").headers["Location"].endswith("/")
   and anon.get("/ask").status_code == anon.get("/dashboard").status_code, "a signed-out visitor is sent to sign-in, like every other page")
js = open(os.path.join(os.path.dirname(appmod.__file__), "static", "app.js"), encoding="utf-8").read()
ok('/api/rag/status' in js and 'href = "/ask"' in js and "error.status" in js, "the sidebar link and the status/data on errors are in the shared script")

# --- sign-in and CSRF guards ------------------------------------------------------------------------
ok(post(anon, ASK, QUESTION).status_code == 401, "asking while signed out is refused (401)")
ok(post(anon, "/api/rag/consent", {}).status_code == 401, "...and so is giving consent")
ok(post(anon, "/api/rag/reindex", {"meeting_id": m_owner}).status_code == 401, "...and re-indexing")
no_header = login(appmod.app.test_client(), "owner@corp.com")
del no_header.environ_base["HTTP_X_CT_REQUEST"]
for path in (ASK, "/api/rag/consent", "/api/rag/reindex"):
    ok(post(no_header, path, {"question": "x", "meeting_id": 1}).status_code == 403, f"{path} without the request header is refused (403)")

# --- checking the question ------------------------------------------------------------------------------
for label, kwargs in [("a missing question", {"json": {}}), ("an empty question", {"json": {"question": ""}}), ("a blank question", {"json": {"question": "  \n "}}),
                      ("a number", {"json": {"question": 123}}), ("a list", {"json": {"question": ["a"]}}), ("null", {"json": {"question": None}}),
                      ("a question that is too long", {"json": {"question": "retest " * 200}}),
                      ("a body that isn't JSON", {"data": "just text", "content_type": "text/plain"})]:
    r = c.post(ASK, **kwargs)
    ok(r.status_code == 400 and r.get_json()["error"], f"{label} -> 400 with a message")
calls_before = len(chat_calls)
ok(c.post(ASK, json={"question": "x" * 1000}).status_code == 200, "a question of exactly 1,000 characters is accepted")
ok(len(chat_calls) == calls_before, "...and the rejected questions never reached the AI")

# --- answering --------------------------------------------------------------------------------------------
r = c.post(ASK, json=QUESTION)
body = r.get_json()
ok(r.status_code == 200 and body["answered"] is True and body["answer"] == "Booked for Wednesday [1].", "a relevant question is answered")
ok(len(body["citations"]) == 1 and body["citations"][0]["meeting_title"] == "Vendor review" and body["citations"][0]["number"] == 1
   and "retest" in body["citations"][0]["text"], "...with the citation, ready for the page to show")
ok(body["engine"] == "openai_compatible · Test Model" and body["context"] and body["query_id"], "...the engine's name, the excerpts the AI saw and the log id")
ok("api_key" not in r.get_data(as_text=True) and "sk-" not in r.get_data(as_text=True), "the response never contains credentials")
ok(chat_calls[-1]["settings"]["config"]["model"] == "Test Model", "the account's saved engine is the one that was used")

r = c.post(ASK, json={"question": "What is the cafeteria menu for Friday?"})
ok(r.status_code == 200 and r.get_json()["answered"] is False and "could not find" in r.get_json()["answer"], "a question the meetings don't cover is a normal 200, not an error")

d = appmod.get_db()
logged = d.execute("SELECT question, answer FROM rag_queries WHERE user_email = 'owner@corp.com' ORDER BY id").fetchall()
d.close()
ok(len(logged) >= 2 and logged[-1]["question"] == "What is the cafeteria menu for Friday?", "questions asked through the route are logged")

# --- failures ---------------------------------------------------------------------------------------------------
chat_behaviour["error"] = RagError("The AI provider rejected the API key.")
r = c.post(ASK, json=QUESTION)
ok(r.status_code == 502 and r.get_json() == {"error": "The AI provider rejected the API key."}, "an AI failure is a 502 with its readable message")
chat_behaviour["error"] = None

noengine = login(appmod.app.test_client(), "noengine@corp.com")
add_meeting("noengine@corp.com", "Vendor review", "Person A: Book the vendor retest for Wednesday.")
r = noengine.post(ASK, json=QUESTION)
ok(r.status_code == 502 and "No AI engine is saved" in r.get_json()["error"], "an account with no engine saved is told how to set one up")
ok(noengine.post(ASK, json={"question": "What is the cafeteria menu for Friday?"}).status_code == 200, "...but a question with no relevant meetings needs no engine")
r = noengine.post("/api/rag/consent", json={})
ok(r.status_code == 409 and "No AI engine is saved" in r.get_json()["error"], "consent can't be given ahead of time for an engine that isn't saved")

# --- the one-time consent for a cloud engine ------------------------------------------------------------------------
cloud = login(appmod.app.test_client(), "cloud@corp.com")
add_meeting("cloud@corp.com", "Vendor review", "Person A: Book the vendor retest for Wednesday. CLOUD-ONLY-TEXT")
save_engine("cloud@corp.com", CLOUD)
calls_before = len(chat_calls)
r = cloud.post(ASK, json=QUESTION)
body = r.get_json()
ok(r.status_code == 409 and body["consent_required"] is True and body["provider"] == "openai_compatible" and body["provider_label"], "a cloud engine without consent gets a 409 the page can act on")
ok(len(chat_calls) == calls_before, "...and nothing was sent to the AI")
ok("sk-secret" not in r.get_data(as_text=True), "...nor is the key in the response")
r = cloud.post("/api/rag/consent", json={})
ok(r.status_code == 200 and r.get_json()["needed"] is True, "confirming records the consent")
r = cloud.post(ASK, json=QUESTION)
ok(r.status_code == 200 and r.get_json()["answered"] is True and len(chat_calls) == calls_before + 1, "the same question is then answered")
ok(cloud.post("/api/rag/consent", json={}).get_json() == {"ok": True, "needed": False}, "confirming again changes nothing")
ok(c.post("/api/rag/consent", json={}).get_json()["needed"] is False, "a local engine never needs consent")

# --- one account never sees another's meetings ------------------------------------------------------------------------------
mine = c.post(ASK, json=QUESTION).get_data(as_text=True)
theirs = cloud.post(ASK, json=QUESTION).get_data(as_text=True)
ok("OWNER-ONLY-TEXT" in mine and "CLOUD-ONLY-TEXT" not in mine, "an account's answer is built from its own meetings only")
ok("CLOUD-ONLY-TEXT" in theirs and "OWNER-ONLY-TEXT" not in theirs, "...and so is the other account's")
ok("CLOUD-ONLY-TEXT" not in json.dumps([call["messages"] for call in chat_calls if "OWNER-ONLY-TEXT" in json.dumps(call["messages"])]),
   "no AI prompt ever held both accounts' text")

# --- re-indexing ---------------------------------------------------------------------------------------------------------------
d = appmod.get_db()
delete_index(d, "owner@corp.com", [m_owner])
d.commit()
gone = d.execute("SELECT COUNT(*) FROM rag_chunks WHERE meeting_id = ?", (m_owner,)).fetchone()[0]
d.close()
r = c.post("/api/rag/reindex", json={"meeting_id": m_owner})
d = appmod.get_db()
back = d.execute("SELECT COUNT(*) FROM rag_chunks WHERE meeting_id = ?", (m_owner,)).fetchone()[0]
d.close()
ok(gone == 0 and r.status_code == 200 and r.get_json() == {"ok": True, "chunks_indexed": back} and back >= 1, "re-indexing rebuilds a meeting's chunks and reports how many")
for label, payload, status in [("a missing meeting_id", {}, 400), ("a text meeting_id", {"meeting_id": "5"}, 400), ("true as the id", {"meeting_id": True}, 400),
                               ("a list", {"meeting_id": [1]}, 400), ("a meeting that doesn't exist", {"meeting_id": 99999}, 404),
                               ("another account's meeting", {"meeting_id": m_owner}, 404)]:
    client = cloud if label == "another account's meeting" else c
    ok(client.post("/api/rag/reindex", json=payload).status_code == status, f"re-indexing {label} -> {status}")
d = appmod.get_db()
ok(d.execute("SELECT COUNT(*) FROM rag_chunks WHERE meeting_id = ?", (m_owner,)).fetchone()[0] == back, "...and the other account's attempt changed nothing")
d.close()
