"""Retrieval (Step 1.4): finds the chunks of one account's meetings that best match a question.

Two searches run side by side and their rankings are merged:
  * vector search: cosine similarity between the question's embedding and every stored chunk vector.
    Finds chunks that mean the same thing in different words.
  * keyword search: SQLite FTS5 with bm25 ranking. Finds exact words that embeddings handle poorly
    (names, ticket ids, part numbers, acronyms).
Reciprocal rank fusion then merges the two lists, so a chunk found by both ranks first.

Privacy: the account's owner_email is part of the SQL WHERE clause of every query here, never applied
afterwards in Python, so another account's chunks are never even read.
"""

import heapq
import re
from dataclasses import dataclass

import numpy as np

from .schema import has_table

POOL = 20             # how many candidates each search contributes before merging
RRF_K = 60            # reciprocal rank fusion constant; 60 is the value from the original paper
MAX_QUERY_WORDS = 20
STOPWORDS = frozenset(
    "a an and are as at be but by did do does for from had has have how i if in is it its of on or "
    "so than that the their then there these they this to was we were what when where which who whom "
    "why will with would you your about can could should".split()
)


@dataclass(frozen=True)
class Hit:
    chunk_id: int
    meeting_id: int
    kind: str                     # 'transcript' or 'commitment'
    speaker: str | None
    text: str
    meeting_title: str | None
    meeting_date: str | None
    turn_start: int | None
    turn_end: int | None
    score: float                  # fused score; only meaningful for ordering the hits
    vector_score: float | None    # cosine similarity (-1..1); None if the chunk has no vector from the current model
    keyword_rank: int | None      # 1 = best keyword match; None if no question word appears in the chunk


def reciprocal_rank_fusion(rankings, k=RRF_K):
    """Merges ranked lists of ids, best first, into one list of (id, score), best first.
    An id scores the sum of 1 / (k + rank) over the lists it appears in, rank counting from 1, so an id
    that appears in several lists, or near the top of one, wins. Ties are broken by id, so the result
    is deterministic."""
    scores = {}
    for ranking in rankings:
        for rank, item in enumerate(ranking, start=1):
            scores[item] = scores.get(item, 0.0) + 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda pair: (-pair[1], pair[0]))


def words_in(text):
    """The lower-case words (letters and digits only) of a text, in order."""
    return re.findall(r"[^\W_]+", text.lower())


def query_words(question):
    """The distinct words of a question that carry meaning: no stop words, at most MAX_QUERY_WORDS."""
    return [w for w in dict.fromkeys(words_in(question)) if w not in STOPWORDS][:MAX_QUERY_WORDS]


def _fts_query(question):
    """Turns free text into a safe FTS5 query: its words, each in double quotes so FTS5 can't read them
    as operators, joined with OR. None if no words remain."""
    return " OR ".join(f'"{w}"' for w in query_words(question)) or None


def _keyword_ranking(db, email, question, limit=POOL):
    """Ids of this account's chunks that contain question words, best bm25 match first."""
    query = _fts_query(question)
    if query is None:
        return []
    rows = db.execute(
        "SELECT rag_fts.rowid FROM rag_fts JOIN rag_chunks c ON c.id = rag_fts.rowid "
        "WHERE rag_fts MATCH ? AND c.owner_email = ? ORDER BY bm25(rag_fts) LIMIT ?",
        (query, email, limit),
    ).fetchall()
    return [row[0] for row in rows]


def _vector_scores(db, email, query_vector, model_name):
    """{chunk id: cosine similarity} for every chunk of this account embedded with the same model and
    size as the question. Vectors from another model are skipped: their numbers mean something else."""
    rows = db.execute(
        "SELECT e.chunk_id, e.vector FROM rag_embeddings e JOIN rag_chunks c ON c.id = e.chunk_id "
        "WHERE c.owner_email = ? AND e.model = ? AND e.dim = ?",
        (email, model_name, len(query_vector)),
    ).fetchall()
    if not rows:
        return {}
    matrix = np.vstack([np.frombuffer(row[1], dtype=np.float32) for row in rows])
    query = np.asarray(query_vector, dtype=np.float32)
    norms = np.linalg.norm(matrix, axis=1) * (np.linalg.norm(query) or 1.0)
    cosine = (matrix @ query) / np.where(norms == 0, 1.0, norms)
    return {row[0]: float(score) for row, score in zip(rows, cosine)}


def _top_ids(scores, limit=POOL):
    return heapq.nlargest(limit, scores, key=lambda chunk_id: (scores[chunk_id], -chunk_id))


def _load_chunks(db, email, ids):
    if not ids:
        return {}
    rows = db.execute(
        "SELECT id, meeting_id, kind, speaker, text, meeting_title, meeting_date, turn_start, turn_end "
        f"FROM rag_chunks WHERE owner_email = ? AND id IN ({','.join('?' * len(ids))})",
        [email, *ids],
    ).fetchall()
    return {row[0]: row for row in rows}


def _resolve_embedder(embed_fn, model_name):
    if embed_fn is None:
        from . import llm  # imported here: litellm is slow to load
        return llm.embed, model_name or llm.EMBEDDING_MODEL_NAME
    return embed_fn, model_name or "unknown"


def search(db, email, question, k=6, embed_fn=None, model_name=None):
    """The k best-matching chunks of this account's meetings for the question, best first, as Hits.
    Returns [] for a blank question or if nothing has been indexed yet.
    embed_fn(list[str]) -> list of vectors and model_name default to the app's embedding model;
    tests pass a stub. Raises RagError if the embedding model can't run."""
    question = (question or "").strip()
    if not question or not has_table(db, "rag_chunks"):
        return []
    embed_fn, model_name = _resolve_embedder(embed_fn, model_name)
    scores = _vector_scores(db, email, embed_fn([question])[0], model_name)
    keyword_ids = _keyword_ranking(db, email, question)
    fused = reciprocal_rank_fusion([_top_ids(scores), keyword_ids])[:k]
    chunks = _load_chunks(db, email, [chunk_id for chunk_id, _ in fused])
    keyword_rank = {chunk_id: rank for rank, chunk_id in enumerate(keyword_ids, start=1)}
    return [
        Hit(chunk_id, *chunks[chunk_id][1:], score=score, vector_score=scores.get(chunk_id),
            keyword_rank=keyword_rank.get(chunk_id))
        for chunk_id, score in fused if chunk_id in chunks
    ]
