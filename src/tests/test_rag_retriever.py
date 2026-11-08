"""RAG retrieval (rag/retriever.py, Step 1.4): rank merging, safe keyword queries, vector and keyword
search, account isolation, and model mismatches. Temporary databases and a stub embedder: nothing is
downloaded and nothing leaves the machine."""

import sqlite3

from _support import ROOT, ok

from rag.indexer import delete_index, index_meeting
from rag.schema import ensure_schema
from rag.retriever import Hit, POOL, _fts_query, _keyword_ranking, _load_chunks, reciprocal_rank_fusion, search

SCHEMA = (ROOT / "schema.sql").read_text(encoding="utf-8")
CONCEPTS = ("retest", "budget", "hiring")
SYNONYMS = {"recruitment": "hiring", "recruiting": "hiring"}  # different words, same meaning


def concept_embed(texts):
    """A stand-in embedder with controlled geometry: one axis per concept (a word or a synonym of it), so
    the cosine similarity between a question and a chunk is known in advance."""
    def normalised(text):
        text = text.lower()
        for synonym, word in SYNONYMS.items():
            text = text.replace(synonym, word)
        return text
    return [[float(word in normalised(t)) for word in CONCEPTS] + [0.05] for t in texts]


def new_db():
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA)
    return db


def add_meeting(db, owner, title, transcript):
    return db.execute(
        "INSERT INTO meetings (owner_email, title, meeting_date, created_at, source, transcript_excerpt) "
        "VALUES (?, ?, '2026-11-01', '2026-11-01T00:00:00', 'upload', ?)", (owner, title, transcript)).lastrowid


def index(db, meeting_id, embed=concept_embed, model="stub"):
    index_meeting(db, meeting_id, embed_fn=embed, model_name=model)


def find(db, owner, question, **kw):
    return search(db, owner, question, embed_fn=kw.pop("embed", concept_embed), model_name=kw.pop("model", "stub"), **kw)


def ids(hits):
    return [h.chunk_id for h in hits]


# --- reciprocal rank fusion (pure) ------------------------------------------------------
fused = reciprocal_rank_fusion([[1, 2, 3], [3, 2, 1]])
scores = dict(fused)
ok(abs(scores[2] - 2 / 62) < 1e-12 and abs(scores[1] - (1 / 61 + 1 / 63)) < 1e-12, "scores are the sum of 1 / (60 + rank)")
ok(scores[1] == scores[3] and [i for i, _ in fused] == [1, 3, 2], "mirror-image rankings tie, broken by id; an item first in one list and last in the other edges out one that is second in both")
ok([i for i, _ in reciprocal_rank_fusion([[1, 2], [2, 3]])] == [2, 1, 3], "an item found by both lists beats items found by only one")
ok([i for i, _ in reciprocal_rank_fusion([[5, 6, 7]])] == [5, 6, 7], "a single list keeps its order")
ok([i for i, _ in reciprocal_rank_fusion([[1, 2], [9]])][:3] == [1, 9, 2], "an item only in a short list still ranks by its position")
ok(reciprocal_rank_fusion([]) == [] and reciprocal_rank_fusion([[], []]) == [], "no lists, or empty lists, give nothing")
ok(reciprocal_rank_fusion([[1, 2], [2, 3]], k=1)[0][0] == 2, "an item in two lists wins whatever k is")
ok(reciprocal_rank_fusion([[3, 1], [2, 4]]) == reciprocal_rank_fusion([[3, 1], [2, 4]]), "the same input always gives the same order")
ok(all(a[1] >= b[1] for a, b in zip(fused, fused[1:])), "scores never increase down the list")

# --- keyword queries are safe ---------------------------------------------------------------
ok(_fts_query("When is the vendor retest?") == '"vendor" OR "retest"', "stop words and punctuation are dropped, words are quoted")
ok(_fts_query("What did we") is None and _fts_query("???") is None and _fts_query("") is None, "no usable words -> no query")
ok(_fts_query('He said "hi" -- DROP (x)*') == '"he" OR "said" OR "hi" OR "drop" OR "x"',
   "quotes, dashes and brackets can't leak into the query")
ok(_fts_query("retest AND budget OR NEAR(x) col:y ^z") == '"retest" OR "budget" OR "near" OR "x" OR "col" OR "y" OR "z"',
   "FTS operators typed in a question become plain words")
ok(_fts_query("ticket 4521 RETEST-42") == '"ticket" OR "4521" OR "retest" OR "42"', "numbers and ids are kept, case is folded")
ok(_fts_query("café retest") == '"café" OR "retest"', "accented letters are kept")
ok(_fts_query("___ retest") == '"retest"', "underscores alone aren't words")
ok(_fts_query("retest retest retest") == '"retest"', "repeated words are queried once")
ok(len(_fts_query(" ".join(f"word{i}" for i in range(100))).split(" OR ")) == 20, "a very long question is capped at 20 words")

# --- search: setup -----------------------------------------------------------------------------
db = new_db()
m_retest = add_meeting(db, "a@example.com", "Vendor review", "Person A: Book the vendor retest for Wednesday.\nPerson B: Yes, the retest is booked.")
m_budget = add_meeting(db, "a@example.com", "Finance sync", "Person A: The budget needs one more approval.\nPerson C: I will chase the budget sign-off.")
m_hiring = add_meeting(db, "a@example.com", "People planning", "Person B: Hiring starts in January.\nPerson C: Ticket ZX-9917 covers the hiring plan.")
m_other = add_meeting(db, "b@example.com", "Someone else's meeting", "Person A: Book the vendor retest for Wednesday.")
for m in (m_retest, m_budget, m_hiring, m_other):
    index(db, m)

ok(find(db, "a@example.com", "") == [] and find(db, "a@example.com", "   \n") == [] and find(db, "a@example.com", None) == [],
   "a blank question returns nothing, even though every chunk has some similarity to it")
ok(find(db, "a@example.com", "a question with real words") != [], "...while a real question on the same data does return chunks")
ok(find(new_db(), "a@example.com", "retest") == [], "searching before anything is indexed returns nothing, without error")

# keyword ranking order: more of the question's words, and rarer words, rank higher
kdb = new_db()
ensure_schema(kdb)
for chunk_id, text in [(1, "retest alone here"), (2, "vendor retest"), (3, "vendor alone here"), (4, "nothing relevant")]:
    kdb.execute("INSERT INTO rag_chunks (id, meeting_id, owner_email, kind, text) VALUES (?, 1, 'a@example.com', 'transcript', ?)", (chunk_id, text))
    kdb.execute("INSERT INTO rag_fts (rowid, chunk_id, text) VALUES (?, ?, ?)", (chunk_id, chunk_id, text))
ranked = _keyword_ranking(kdb, "a@example.com", "vendor retest")
ok(ranked[0] == 2 and set(ranked) == {1, 2, 3}, "a chunk containing both question words ranks above chunks with one, and non-matching chunks are left out")

# --- search: ranking and result contents ------------------------------------------------
hits = find(db, "a@example.com", "when is the vendor retest")
ok(hits and isinstance(hits[0], Hit) and hits[0].meeting_id == m_retest, "the chunk about the retest comes first")
ok(abs(hits[0].vector_score - 1.0) < 0.01 or hits[0].vector_score > 0.95, "its cosine similarity with the question is near 1")
ok(hits[0].keyword_rank == 1, "it is also the best keyword match")
top = hits[0]
ok(top.meeting_title == "Vendor review" and top.meeting_date == "2026-11-01" and top.kind == "transcript"
   and top.speaker == "Person A, Person B" and (top.turn_start, top.turn_end) == (0, 1) and "retest" in top.text,
   "a hit carries what a citation needs: title, date, speakers, turns and text")
ok(all(a.score >= b.score for a, b in zip(hits, hits[1:])), "hits are ordered best first")
ok(len(find(db, "a@example.com", "retest", k=1)) == 1 and len(find(db, "a@example.com", "retest", k=50)) <= 3, "k limits the number of hits, and fewer chunks than k is fine")

# the three paths: found by both, by vector only, by keyword only
both = find(db, "a@example.com", "budget")
ok(both[0].meeting_id == m_budget and both[0].vector_score is not None and both[0].keyword_rank is not None, "a chunk found by both searches ranks first")

only_vector = find(db, "a@example.com", "recruitment")  # shares no word with "Hiring starts in January", but means the same
ok(only_vector[0].meeting_id == m_hiring and only_vector[0].keyword_rank is None and only_vector[0].vector_score > 0.95,
   "a chunk that means the same thing but shares no word is found, and ranks first, by vector search alone")
only_keyword = find(db, "a@example.com", "ZX-9917")                         # an exact id: its vector is no closer than any other chunk's
ok(only_keyword and only_keyword[0].meeting_id == m_hiring and only_keyword[0].keyword_rank == 1,
   "an exact id is found by keyword search even though the embedding knows nothing about it")
ok(find(db, "a@example.com", "ZX-9917", k=1)[0].text.count("ZX-9917") == 1, "...and it comes back with the matching text")

vector_only = [h for h in find(db, "a@example.com", "retest") if h.keyword_rank is None]
ok(all(h.vector_score is not None for h in vector_only), "a hit that matched no word still has its similarity score")

# --- account isolation ---------------------------------------------------------------------------
mine = find(db, "a@example.com", "vendor retest Wednesday", k=10)
theirs = find(db, "b@example.com", "vendor retest Wednesday", k=10)
ok(m_other not in {h.meeting_id for h in mine} and {h.meeting_id for h in theirs} == {m_other},
   "each account only ever gets its own meetings back, even for identical words and identical vectors")
ok(find(db, "nobody@example.com", "retest", k=10) == [], "an account with no meetings gets nothing")
ok(not any(h.chunk_id in ids(theirs) for h in mine), "no chunk is shared between the two result sets")
their_chunk = db.execute("SELECT id FROM rag_chunks WHERE owner_email = 'b@example.com'").fetchone()[0]
ok(_load_chunks(db, "a@example.com", [their_chunk]) == {} and _load_chunks(db, "b@example.com", [their_chunk]) != {},
   "even given another account's chunk id, loading chunks refuses it (a second line of defence)")
kw_ids = _keyword_ranking(db, "a@example.com", "retest")
ok(all(db.execute("SELECT owner_email FROM rag_chunks WHERE id = ?", (i,)).fetchone()[0] == "a@example.com" for i in kw_ids),
   "the keyword search itself only reads the account's own chunks")

# --- safe with awkward questions -----------------------------------------------------------------
for question in ['He said "retest" -- DROP TABLE rag_chunks; (x)*', "retest AND NOT budget OR NEAR(a b)", "\"unbalanced", "col:retest ^budget", "' OR 1=1 --", "retest\n\tbudget"]:
    try:
        find(db, "a@example.com", question)
        ok(True, f"no error for the question {question!r}")
    except Exception as e:  # noqa: BLE001
        ok(False, f"error for the question {question!r}: {e}")
ok(db.execute("SELECT COUNT(*) FROM rag_chunks").fetchone()[0] > 0, "...and the tables are untouched afterwards")

# --- vectors and models ---------------------------------------------------------------------------
scaled = new_db()
sm = add_meeting(scaled, "a@example.com", "Scaled", "Person A: The retest is on Friday.")
index(scaled, sm, embed=lambda texts: [[v * 7.0 for v in vec] for vec in concept_embed(texts)])
ok(abs(find(scaled, "a@example.com", "retest")[0].vector_score - 1.0) < 0.01, "similarity doesn't depend on a vector's length")

mixed = new_db()
mm = add_meeting(mixed, "a@example.com", "Old model", "Person A: The retest is on Friday.")
index(mixed, mm, model="old-model")
ok(find(mixed, "a@example.com", "retest", model="new-model")[0].vector_score is None, "a chunk embedded by another model is not compared by vector...")
ok(find(mixed, "a@example.com", "retest", model="new-model")[0].keyword_rank == 1, "...but is still found by keyword")
ok(find(mixed, "a@example.com", "retest", model="old-model")[0].vector_score > 0.95, "with the matching model it is compared")

odd_dim = new_db()
dm = add_meeting(odd_dim, "a@example.com", "Other size", "Person A: The retest is on Friday.")
index(odd_dim, dm, embed=lambda texts: [[1.0, 0.0] for _ in texts])
ok(find(odd_dim, "a@example.com", "retest")[0].vector_score is None, "vectors of a different size are skipped, not crashed on")

# --- stays current with the index ----------------------------------------------------------------
delete_index(db, "a@example.com", [m_retest])
ok(m_retest not in {h.meeting_id for h in find(db, "a@example.com", "vendor retest Wednesday", k=10)}, "a deleted meeting is no longer found")
before = [h.chunk_id for h in find(db, "a@example.com", "budget")]
index(db, m_budget)
ok([h.meeting_id for h in find(db, "a@example.com", "budget")][:1] == [m_budget] and [h.chunk_id for h in find(db, "a@example.com", "budget")] != before,
   "a re-indexed meeting is found under its new chunk ids")
ok(POOL == 20, "each search contributes its 20 best candidates")
