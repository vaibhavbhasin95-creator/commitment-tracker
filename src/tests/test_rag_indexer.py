"""RAG indexing (rag/indexer.py, Step 1.3) and its wiring into app.py: index_meeting, delete_index,
the index_meeting job, and the delete / extraction hooks. Temporary databases, a stub in place of the
embedding model: nothing is downloaded and nothing leaves the machine."""

import os
import sqlite3

import numpy as np

os.environ["RAG_ENABLED"] = "1"  # before _support / app are imported: other suites keep indexing off

from _support import ROOT, fresh_app, ok  # noqa: E402

import transcript_parser as tp
from rag.errors import MeetingNotFound, RagError
from rag.indexer import delete_index, index_meeting

SCHEMA = (ROOT / "schema.sql").read_text(encoding="utf-8")
TRANSCRIPT = ("Person A: Can you book the vendor retest for Wednesday?\n"
              "Person B: Yes, I will book the retest for Wednesday.\n"
              "Person A: Great, thanks.")


class StubEmbed:
    """Stands in for the embedding model: 4-number vectors, and a record of every call."""

    def __init__(self):
        self.calls = []

    def __call__(self, texts):
        self.calls.append(list(texts))
        return [[float(len(t) % 7), 1.0, 0.5, float(i)] for i, t in enumerate(texts)]


def new_db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def add_meeting(db, owner="a@example.com", title="Vendor review", transcript=TRANSCRIPT):
    return db.execute(
        "INSERT INTO meetings (owner_email, title, meeting_date, created_at, source, transcript_excerpt) "
        "VALUES (?, ?, '2026-11-01', '2026-11-01T00:00:00', 'upload', ?)", (owner, title, transcript)).lastrowid


def add_commitment(db, meeting_id, task="Book the vendor retest", owner="Person B", status="pending", quote=None):
    db.execute("INSERT INTO commitments (meeting_id, task_text, owner_name, due_date, status, source_quote, created_at) "
               "VALUES (?, ?, ?, '2026-11-04', ?, ?, '2026-11-01T00:00:00')", (meeting_id, task, owner, status, quote or task))


def counts(db):
    return tuple(db.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in ("rag_chunks", "rag_embeddings", "rag_fts"))


def chunks_of(db, meeting_id):
    return [dict(r) for r in db.execute("SELECT * FROM rag_chunks WHERE meeting_id = ? ORDER BY id", (meeting_id,))]


# --- index_meeting: what gets stored --------------------------------------------
db, embed = new_db(), StubEmbed()
m1 = add_meeting(db)
add_commitment(db, m1)
n = index_meeting(db, m1, embed_fn=embed, model_name="stub")
rows = chunks_of(db, m1)
ok(n == len(rows) == 2, "a short transcript and one commitment -> two chunks")
ok(sorted(r["kind"] for r in rows) == ["commitment", "transcript"], "one transcript chunk and one commitment chunk")
ok(counts(db) == (2, 2, 2), "every chunk has exactly one vector and one keyword row")
ok(len(embed.calls) == 1 and len(embed.calls[0]) == 2, "all chunks are embedded in one batch, not one at a time")
ok(all(r["owner_email"] == "a@example.com" and r["meeting_title"] == "Vendor review" and r["meeting_date"] == "2026-11-01" for r in rows),
   "every chunk carries its owner (the privacy filter), meeting title and date")

transcript_chunk = next(r for r in rows if r["kind"] == "transcript")
ok(transcript_chunk["text"].startswith("Person A: Can you book") and "Meeting:" not in transcript_chunk["text"],
   "the stored chunk text is clean, with no context header")
ok(any(t.startswith("Meeting: Vendor review (2026-11-01)\n") for t in embed.calls[0]), "the text that is embedded starts with the meeting header")
ok(transcript_chunk["speaker"] == "Person A, Person B" and (transcript_chunk["turn_start"], transcript_chunk["turn_end"]) == (0, 2),
   "a transcript chunk records its speakers and turn range")

commitment_chunk = next(r for r in rows if r["kind"] == "commitment")
ok("Commitment: Book the vendor retest" in commitment_chunk["text"] and "Owner: Person B" in commitment_chunk["text"]
   and "Due: 2026-11-04" in commitment_chunk["text"] and "Status: pending" in commitment_chunk["text"],
   "a commitment chunk spells out task, owner, due date and status")
ok(commitment_chunk["speaker"] == "Person B" and commitment_chunk["turn_start"] is None, "a commitment chunk has its owner as speaker and no turns")

emb = db.execute("SELECT * FROM rag_embeddings WHERE chunk_id = ?", (transcript_chunk["id"],)).fetchone()
ok(emb["model"] == "stub" and emb["dim"] == 4, "the embedding model name and dimension are stored")
ok(np.frombuffer(emb["vector"], dtype=np.float32).shape == (4,), "the vector is stored as float32 bytes and reads back")
ok(db.execute("SELECT rowid FROM rag_fts WHERE rag_fts MATCH '\"retest\"' ORDER BY rowid").fetchall() != []
   and {r[0] for r in db.execute("SELECT rowid FROM rag_fts WHERE rag_fts MATCH '\"retest\"'")} <= {r["id"] for r in rows},
   "keyword search finds the chunk, and the keyword row's id is the chunk id")

# --- index_meeting: replacing, failing, and edge cases -----------------------------
ids_before = [r["id"] for r in chunks_of(db, m1)]
index_meeting(db, m1, embed_fn=embed, model_name="stub")
ok(counts(db) == (2, 2, 2), "indexing the same meeting again leaves no duplicates")
ok([r["id"] for r in chunks_of(db, m1)] != ids_before, "re-indexing replaces the old chunks")
ok(min(r["id"] for r in chunks_of(db, m1)) > max(ids_before), "chunk ids are never reused, so ids logged with an old answer can't point at new text")

db.execute("UPDATE commitments SET status = 'done' WHERE meeting_id = ?", (m1,))
index_meeting(db, m1, embed_fn=embed, model_name="stub")
ok("Status: done" in next(r for r in chunks_of(db, m1) if r["kind"] == "commitment")["text"], "re-indexing picks up a changed commitment status")


def broken_embed(texts):
    raise RagError("embedding model unavailable")


before = (counts(db), [r["id"] for r in chunks_of(db, m1)])
try:
    index_meeting(db, m1, embed_fn=broken_embed, model_name="stub")
    ok(False, "an embedding failure is reported")
except RagError:
    ok(True, "an embedding failure is reported")
ok((counts(db), [r["id"] for r in chunks_of(db, m1)]) == before, "a failed re-index leaves the previous index untouched")

try:
    index_meeting(db, m1, embed_fn=lambda texts: [[1.0]], model_name="stub")
    ok(False, "a wrong number of vectors is refused")
except RagError:
    ok(True, "a wrong number of vectors is refused")

# A failure in the middle of writing (here: vectors that can't be stored) happens after the old rows were deleted.
before = (counts(db), [r["id"] for r in chunks_of(db, m1)])
try:
    index_meeting(db, m1, embed_fn=lambda texts: [object()] * len(texts), model_name="stub")  # len(vector) fails mid-write
    ok(False, "a failure while writing is reported")
except TypeError:
    ok(True, "a failure while writing is reported")
ok((counts(db), [r["id"] for r in chunks_of(db, m1)]) == before, "a failure while writing rolls back, so the old index survives intact")

empty_db, empty_embed = new_db(), StubEmbed()
m_empty = add_meeting(empty_db, transcript="")
ok(index_meeting(empty_db, m_empty, embed_fn=empty_embed, model_name="stub") == 0 and counts(empty_db) == (0, 0, 0) and not empty_embed.calls,
   "a meeting with no transcript and no commitments stores nothing and never calls the embedder")

m_only_tasks = add_meeting(empty_db, transcript=None)
add_commitment(empty_db, m_only_tasks)
ok(index_meeting(empty_db, m_only_tasks, embed_fn=empty_embed, model_name="stub") == 1
   and chunks_of(empty_db, m_only_tasks)[0]["kind"] == "commitment", "a meeting with only commitments still gets a chunk")

try:
    index_meeting(db, 9999, embed_fn=embed, model_name="stub")
    ok(False, "an unknown meeting is reported")
except MeetingNotFound:
    ok(True, "an unknown meeting is reported")

m_quote = add_meeting(db, transcript=None)
add_commitment(db, m_quote, task="Send the deck", quote="Person B: I will send the deck by Friday.")
index_meeting(db, m_quote, embed_fn=embed, model_name="stub")
ok("Said in the meeting: Person B: I will send the deck by Friday." in chunks_of(db, m_quote)[0]["text"],
   "a commitment chunk includes the sentence it came from")

# --- delete_index ---------------------------------------------------------------------
db, embed = new_db(), StubEmbed()
mine1, mine2 = add_meeting(db, "a@example.com", "One"), add_meeting(db, "a@example.com", "Two")
theirs = add_meeting(db, "b@example.com", "Theirs")
for m in (mine1, mine2, theirs):
    add_commitment(db, m)
    index_meeting(db, m, embed_fn=embed, model_name="stub")
ok(counts(db) == (6, 6, 6), "three meetings, two chunks each")

removed = delete_index(db, "a@example.com", [mine1])
ok(removed == 2 and chunks_of(db, mine1) == [] and counts(db) == (4, 4, 4), "deleting a meeting's index removes its chunks, vectors and keyword rows")
ok(len(chunks_of(db, mine2)) == 2 and len(chunks_of(db, theirs)) == 2, "other meetings are untouched")

ok(delete_index(db, "a@example.com", [theirs]) == 0 and len(chunks_of(db, theirs)) == 2, "one account can't delete another account's index")
ok(delete_index(db, "a@example.com", []) == 0, "an empty meeting list deletes nothing")

delete_index(db, "a@example.com", [mine2], kind="commitment")
ok([r["kind"] for r in chunks_of(db, mine2)] == ["transcript"] and counts(db) == (3, 3, 3), "kind='commitment' keeps the transcript chunks")

delete_index(db, "a@example.com")
ok(counts(db) == (2, 2, 2) and len(chunks_of(db, theirs)) == 2, "with no meeting list, everything of that account goes and nobody else's")
ok(db.execute("SELECT COUNT(*) FROM rag_fts WHERE rag_fts MATCH '\"retest\"'").fetchone()[0] == 2, "no orphan keyword rows are left behind")

plain = new_db()
ok(delete_index(plain, "a@example.com", [1]) == 0, "deleting when RAG was never used does nothing")
ok(plain.execute("SELECT COUNT(*) FROM sqlite_master WHERE name LIKE 'rag_%'").fetchone()[0] == 0, "...and does not create the RAG tables")

# --- keeping the index fresh: triggers, sweeper, catch-up -----------------------------------
from rag.indexer import QUIET_SECONDS, mark_unindexed, prepare, sweep_dirty  # noqa: E402
from rag.schema import drop_triggers, ensure_schema  # noqa: E402

OLD = "2000-01-01 00:00:00.000"
FUTURE = "9999-12-31 00:00:00.000"


def dirty(d):
    return {r[0] for r in d.execute("SELECT meeting_id FROM rag_dirty")}


def reset(d):
    d.execute("DELETE FROM rag_dirty")
    d.commit()


def age(d):
    d.execute("UPDATE rag_dirty SET marked_at = ?", (OLD,))


fdb, fe = new_db(), StubEmbed()
fm = add_meeting(fdb, "a@example.com", "Fresh")
add_commitment(fdb, fm, task="Book the retest")
index_meeting(fdb, fm, embed_fn=fe, model_name="stub")
ok(dirty(fdb) == set(), "right after indexing, a meeting isn't marked out of date")
ok(sorted(r[0] for r in fdb.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")) ==
   ["rag_commitment_added", "rag_commitment_changed", "rag_commitment_removed", "rag_meeting_added", "rag_meeting_changed"],
   "indexing installs the five triggers")

add_commitment(fdb, fm, task="A second task")
ok(dirty(fdb) == {fm}, "adding a task marks its meeting out of date")
reset(fdb)
for column, value in [("status", "done"), ("task_text", "Changed task"), ("owner_name", "Person C"),
                      ("due_date", "2026-12-01"), ("source_quote", "A different quote")]:
    fdb.execute(f"UPDATE commitments SET {column} = ? WHERE meeting_id = ? AND id = (SELECT MIN(id) FROM commitments)", (value, fm))
    ok(dirty(fdb) == {fm}, f"editing a task's {column} marks its meeting out of date")
    reset(fdb)
fdb.execute("DELETE FROM commitments WHERE id = (SELECT MAX(id) FROM commitments)")
ok(dirty(fdb) == {fm}, "deleting a task marks its meeting out of date")
reset(fdb)
for column, value in [("transcript_excerpt", "Person A: New text."), ("title", "New title"), ("meeting_date", "2026-12-24")]:
    fdb.execute(f"UPDATE meetings SET {column} = ? WHERE id = ?", (value, fm))
    ok(dirty(fdb) == {fm}, f"editing a meeting's {column} marks it out of date")
    reset(fdb)
new_id = add_meeting(fdb, "a@example.com", "Brand new")
ok(dirty(fdb) == {new_id}, "a new meeting is marked out of date")
reset(fdb)

fdb.execute("UPDATE commitments SET owner_email = 'x@example.com', external_ref = 'K-1', updated_at = 't', owner_match = 'manual'")
fdb.execute("UPDATE meetings SET transcript_provider = 'zoom'")
fdb.execute("UPDATE commitments SET status = status")
ok(dirty(fdb) == set(), "changes that aren't searchable (email, sync reference, an edit that changes nothing) don't mark anything")
for status in ("in_progress", "done", "pending"):
    fdb.execute("UPDATE commitments SET status = ?", (status,))
ok(dirty(fdb) == {fm} and fdb.execute("SELECT COUNT(*) FROM rag_dirty").fetchone()[0] == 1, "a burst of edits leaves one mark for the meeting")
reset(fdb)

fdb.execute("INSERT INTO rag_dirty VALUES (?, ?)", (fm, OLD))
fdb.execute("INSERT INTO rag_dirty VALUES (?, ?)", (new_id, FUTURE))
index_meeting(fdb, fm, embed_fn=fe, model_name="stub")
ok(dirty(fdb) == {new_id}, "indexing clears the meeting's earlier marks")
fdb.execute("INSERT OR REPLACE INTO rag_dirty VALUES (?, ?)", (fm, FUTURE))
index_meeting(fdb, fm, embed_fn=fe, model_name="stub")
ok(fm in dirty(fdb), "a change made after indexing started stays marked")
reset(fdb)

# the sweeper
calls = []
record = lambda d, email, meeting_id: calls.append((email, meeting_id))
fdb.execute("INSERT INTO rag_dirty VALUES (?, strftime('%Y-%m-%d %H:%M:%f', 'now'))", (fm,))
ok(sweep_dirty(fdb, record) == 0 and not calls and dirty(fdb) == {fm}, f"a change that just happened waits out the {QUIET_SECONDS}-second quiet period")
ok(sweep_dirty(fdb, record, quiet_seconds=0) == 1 and calls == [("a@example.com", fm)] and dirty(fdb) == set(),
   "once quiet, a re-index is queued for the meeting's owner and the mark is cleared")

calls.clear()
fdb.execute("INSERT INTO rag_dirty VALUES (?, ?)", (9999, OLD))
ok(sweep_dirty(fdb, record) == 0 and not calls and dirty(fdb) == set(), "a mark for a meeting that no longer exists is dropped without queueing")

for _ in range(5):
    fdb.execute("UPDATE commitments SET task_text = task_text || '!'")
age(fdb)
ok(sweep_dirty(fdb, record) == 1 and len(calls) == 1, "many edits to one meeting queue a single re-index")

reset(fdb)
fdb.execute("INSERT INTO rag_dirty VALUES (?, ?)", (fm, OLD))
edit_during_sweep = lambda d, email, meeting_id: d.execute("UPDATE rag_dirty SET marked_at = ? WHERE meeting_id = ?", (FUTURE, meeting_id))
sweep_dirty(fdb, edit_during_sweep)
ok(dirty(fdb) == {fm}, "a meeting edited again while the sweep runs keeps its newer mark")

reset(fdb)
fdb.execute("INSERT INTO rag_dirty VALUES (?, ?)", (fm, OLD))
fdb.execute("INSERT INTO rag_dirty VALUES (?, ?)", (new_id, OLD))
fdb.commit()  # the sweeper rolls back its own work on failure; don't leave these inserts pending on the same connection


def second_enqueue_fails(d, email, meeting_id):
    if meeting_id == new_id:  # the sweep goes in meeting-id order, so fm has already been queued and cleared by now
        raise RuntimeError("queue unavailable")


try:
    sweep_dirty(fdb, second_enqueue_fails)
    ok(False, "a failure while queueing is reported")
except RuntimeError:
    ok(True, "a failure while queueing is reported")
ok(dirty(fdb) == {fm, new_id}, "...and every mark is kept, including the one already handled, so the next sweep starts clean")
ok(sweep_dirty(new_db(), record) == 0, "sweeping a database that never used RAG does nothing")

# catch-up for meetings that were never indexed
cdb, ce = new_db(), StubEmbed()
with_text, only_tasks, empty, indexed = (add_meeting(cdb, "a@example.com", "T"), add_meeting(cdb, "a@example.com", "C", transcript=None),
                                         add_meeting(cdb, "a@example.com", "E", transcript=""), add_meeting(cdb, "a@example.com", "I"))
add_commitment(cdb, only_tasks)
ensure_schema(cdb)
index_meeting(cdb, indexed, embed_fn=ce, model_name="stub")
reset(cdb)
ok(prepare(cdb) == 2 and dirty(cdb) == {with_text, only_tasks}, "prepare marks meetings with content that were never indexed, not empty or indexed ones")
ok(prepare(cdb) == 0, "running prepare again marks nothing new")
index_meeting(cdb, with_text, embed_fn=ce, model_name="stub")
ok(dirty(cdb) == {only_tasks}, "catching up one meeting leaves the others marked")

# deleting
ddb, de = new_db(), StubEmbed()
d1, d2 = add_meeting(ddb, "a@example.com", "One"), add_meeting(ddb, "a@example.com", "Two")
d3 = add_meeting(ddb, "b@example.com", "Other account")
for m in (d1, d2, d3):
    index_meeting(ddb, m, embed_fn=de, model_name="stub")
for m in (d1, d2, d3):
    ddb.execute("INSERT OR REPLACE INTO rag_dirty VALUES (?, ?)", (m, OLD))
delete_index(ddb, "a@example.com", [d1])
ok(dirty(ddb) == {d2, d3}, "deleting a meeting's index also drops its out-of-date mark")
delete_index(ddb, "a@example.com", [d3])
ok(d3 in dirty(ddb), "...but never another account's")
delete_index(ddb, "a@example.com", [d2], kind="commitment")
ok(d2 in dirty(ddb), "removing only the task chunks leaves the meeting marked, so it is rebuilt")

old_schema = new_db()
index_meeting(old_schema, add_meeting(old_schema), embed_fn=StubEmbed(), model_name="stub")
drop_triggers(old_schema)
old_schema.execute("DROP TABLE rag_dirty")
ok(delete_index(old_schema, "a@example.com", [1]) == 1, "deleting still works on a database indexed before out-of-date marks existed")
ensure_schema(old_schema)
ok(has_rows := old_schema.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'rag_dirty'").fetchone()[0] == 1, "the next ensure_schema adds the missing table")

ok(sorted(r[0] for r in old_schema.execute("SELECT name FROM sqlite_master WHERE type = 'trigger'")) != [], "...and the triggers")
drop_triggers(old_schema)
ok(old_schema.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'trigger'").fetchone()[0] == 0, "drop_triggers removes them (to revert the feature)")
bare = sqlite3.connect(":memory:")
ensure_schema(bare)
ok(bare.execute("SELECT COUNT(*) FROM sqlite_master WHERE type = 'trigger'").fetchone()[0] == 0 and
   bare.execute("SELECT COUNT(*) FROM sqlite_master WHERE name = 'rag_dirty'").fetchone()[0] == 1,
   "on a database without meetings and tasks, the tables are made and the triggers are skipped without error")

# --- wiring into app.py -----------------------------------------------------------------
appmod, client, _ = fresh_app("owner@corp.com")
import jobs  # noqa: E402
import rag.llm as rag_llm  # noqa: E402

tp.extract_commitments = lambda text, meeting_date=None, threshold=0.9: []
tp.extract_commitments_from_turns = lambda turns, meeting_date=None, threshold=0.9: []
app_embed = StubEmbed()
rag_llm.embed, rag_llm.EMBEDDING_MODEL_NAME = app_embed, "stub-model"  # the job uses the app's default embedder


def new_meeting(email="owner@corp.com"):
    d = appmod.get_db()
    meeting_id = appmod.create_meeting_row(d, email, "Vendor review", "2026-11-01", TRANSCRIPT, "upload")
    add_commitment(d, meeting_id)
    d.commit()
    d.close()
    return meeting_id


def app_counts():
    d = appmod.get_db()
    result = counts(d)
    d.close()
    return result


mid = new_meeting()
result = appmod.job_index_meeting({"meeting_id": mid}, {})
ok(result == {"chunks_indexed": 2} and app_counts() == (2, 2, 2), "the index_meeting job indexes a meeting with the app's default embedder")
ok(jobs.KIND_LABELS.get("index_meeting") == "Index meeting for search", "the job kind is registered with a readable label")

try:
    appmod.job_index_meeting({"meeting_id": 9999}, {})
    ok(False, "the job reports a deleted meeting")
except jobs.PermanentError:
    ok(True, "the job reports a deleted meeting as permanent, so it isn't retried")

other = new_meeting("someone@else.com")
appmod.job_index_meeting({"meeting_id": other}, {})
ok(app_counts() == (4, 4, 4), "a second account's meeting is indexed alongside")

ok(client.post("/api/commitments/clear", json={"confirm": True, "meeting_id": mid}).status_code == 200, "clearing one meeting's tasks succeeds")
d = appmod.get_db()
ok([r["kind"] for r in chunks_of(d, mid)] == ["transcript"], "clearing a meeting's tasks removes its commitment chunks and keeps the transcript chunks")
d.close()

ok(client.delete(f"/api/meetings/{mid}").status_code == 200, "deleting a meeting succeeds")
d = appmod.get_db()
ok(chunks_of(d, mid) == [] and len(chunks_of(d, other)) == 2, "deleting a meeting removes its index and nobody else's")
ok(counts(d) == (2, 2, 2), "no vectors or keyword rows are left behind by the delete route")
d.close()

keep = new_meeting()
appmod.job_index_meeting({"meeting_id": keep}, {})
ok(client.post("/api/meetings/clear", json={"confirm": True}).status_code == 200, "clearing all meetings succeeds")
d = appmod.get_db()
ok(chunks_of(d, keep) == [] and len(chunks_of(d, other)) == 2, "clearing all meetings removes this account's index and nobody else's")
d.close()

# queueing: skipped in inline (test) mode, queued otherwise, never raises
recorded = []
real_queue = appmod.queue_rag_index
ok(real_queue("owner@corp.com", keep) is None, "no indexing job is queued while jobs run inline (tests)")
os.environ["CT_JOBS_INLINE"] = "0"
try:
    job_id = real_queue("owner@corp.com", other)
    d = appmod.get_db()
    queued = d.execute("SELECT kind, meeting_id, user_email FROM jobs WHERE id = ?", (job_id,)).fetchone()
    d.close()
    ok(queued is not None and tuple(queued) == ("index_meeting", other, "owner@corp.com"), "outside inline mode an index_meeting job is queued for the meeting")
    saved_rag_indexer, appmod.rag_indexer = appmod.rag_indexer, None
    ok(real_queue("owner@corp.com", other) is None, "with RAG turned off nothing is queued")
    appmod.rag_indexer = saved_rag_indexer
finally:
    os.environ["CT_JOBS_INLINE"] = "1"

# extraction triggers indexing, even when extraction itself fails
appmod.queue_rag_index = lambda email, meeting_id: recorded.append((email, meeting_id))
r = client.post("/api/meetings", json={"title": "Upload", "meeting_date": "2026-11-02", "transcript_excerpt": TRANSCRIPT})
ok(r.status_code in (200, 201) and recorded and recorded[-1][0] == "owner@corp.com" and recorded[-1][1] == r.get_json()["id"],
   "an uploaded meeting is queued for indexing once extraction has run")

tp.extract_commitments = lambda *a, **k: (_ for _ in ()).throw(tp.ModelNotReadyError("model not trained"))
recorded.clear()
r = client.post("/api/meetings", json={"title": "Upload 2", "meeting_date": "2026-11-02", "transcript_excerpt": TRANSCRIPT})
ok(recorded and recorded[-1][1] == r.get_json()["id"], "the meeting is queued for indexing even when extraction fails")
appmod.queue_rag_index = real_queue

# --- freshness through the app: a real edit -> marked -> swept -> one job -> index updated ------
ok("search index refresh" in [task[0] for task in jobs._periodic], "the sweeper is registered as a periodic task")
fresh = new_meeting()
appmod.job_index_meeting({"meeting_id": fresh}, {})
d = appmod.get_db()
cid = d.execute("SELECT id FROM commitments WHERE meeting_id = ?", (fresh,)).fetchone()[0]
ok(dirty(d) - {fresh} == dirty(d) and fresh not in dirty(d), "a freshly indexed meeting is not marked")
d.close()


def queued_index_jobs():
    d = appmod.get_db()
    n = d.execute("SELECT COUNT(*) FROM jobs WHERE kind = 'index_meeting' AND meeting_id = ? AND status = 'queued'", (fresh,)).fetchone()[0]
    d.close()
    return n


def age_marks():
    d = appmod.get_db()
    d.execute("UPDATE rag_dirty SET marked_at = ?", (OLD,))
    d.commit()
    d.close()


def commitment_chunk_text():
    d = appmod.get_db()
    text = d.execute("SELECT text FROM rag_chunks WHERE meeting_id = ? AND kind = 'commitment'", (fresh,)).fetchone()[0]
    d.close()
    return text


appmod._rag_prepared = True  # skip the one-time catch-up here; it is checked below
ok(client.patch(f"/api/commitments/{cid}", json={"status": "done"}).status_code == 200, "a task's status is changed through the app")
d = appmod.get_db()
ok(fresh in dirty(d), "that edit marked the meeting out of date, with no change to the route")
d.close()
appmod.rag_sweep()
ok(queued_index_jobs() == 0, "the sweep waits for the quiet period")
age_marks()
appmod.rag_sweep()
ok(queued_index_jobs() == 1, "after the quiet period the sweep queues a re-index job")

ok(client.patch(f"/api/commitments/{cid}", json={"status": "in_progress"}).status_code == 200, "a second edit follows")
age_marks()
appmod.rag_sweep()
ok(queued_index_jobs() == 1, "a re-index already waiting is not queued twice")

appmod.job_index_meeting({"meeting_id": fresh}, {})
ok("Status: in progress" in commitment_chunk_text(), "the re-index picks up the latest status")
d = appmod.get_db()
ok(fresh not in dirty(d), "and the meeting is no longer marked")
d.close()

appmod._rag_prepared = False
appmod.rag_sweep()
ok(appmod._rag_prepared is True, "the first sweep also runs the one-time catch-up")
saved_rag_indexer, appmod.rag_indexer = appmod.rag_indexer, None
appmod.rag_sweep()  # must not raise
appmod.rag_indexer = saved_rag_indexer
ok(True, "the sweep does nothing when RAG is turned off")
