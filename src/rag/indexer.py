"""Indexing (Step 1.3): turns a meeting into chunks, vectors and keyword rows, and removes them again.

index_meeting() reads a meeting's transcript and commitments, chunks the transcript, embeds every
chunk in one batch, then replaces that meeting's rows in rag_chunks, rag_embeddings and rag_fts in a
single transaction. Running it again gives the same result, never duplicates. The slow embedding call
happens before any write, so a failure leaves the previous index untouched.

app.py calls this from a background job and calls delete_index() wherever meetings or tasks are removed.

Keeping the index fresh is automatic: triggers (schema.py) mark a meeting in rag_dirty whenever its
transcript, title, date or commitments change; sweep_dirty() queues one re-index job for each meeting
that has been quiet for QUIET_SECONDS; prepare() marks meetings that were never indexed.
"""

import numpy as np

from .chunking import chunk_transcript
from .errors import MeetingNotFound, RagError
from .schema import ensure_schema, has_table

QUIET_SECONDS = 20  # a meeting is re-indexed once nothing about it has changed for this long


def _tables_exist(db) -> bool:
    return db.execute("SELECT 1 FROM sqlite_master WHERE name = 'rag_chunks'").fetchone() is not None


def delete_index(db, owner_email, meeting_ids=None, kind=None, clear_dirty=True) -> int:
    """Removes this account's indexed chunks, with their vectors and keyword rows.
    meeting_ids limits it to those meetings; kind ('transcript' or 'commitment') to that kind.
    When whole meetings are removed, their out-of-date marks go too (clear_dirty); a re-index
    passes False because the meeting still exists and a change made meanwhile must stay marked.
    Does nothing if RAG has never been used. Leaves committing to the caller. Returns chunks removed."""
    if not _tables_exist(db):
        return 0
    where, params = ["owner_email = ?"], [owner_email]
    if meeting_ids is not None:
        if not meeting_ids:
            return 0
        where.append(f"meeting_id IN ({','.join('?' * len(meeting_ids))})")
        params.extend(meeting_ids)
    if kind:
        where.append("kind = ?")
        params.append(kind)
    condition = " AND ".join(where)
    ids = [(row[0],) for row in db.execute(f"SELECT id FROM rag_chunks WHERE {condition}", params)]
    db.executemany("DELETE FROM rag_fts WHERE rowid = ?", ids)  # the keyword row's rowid is the chunk id
    db.executemany("DELETE FROM rag_embeddings WHERE chunk_id = ?", ids)
    db.execute(f"DELETE FROM rag_chunks WHERE {condition}", params)
    if clear_dirty and meeting_ids and not kind and has_table(db, "rag_dirty") and has_table(db, "meetings"):
        # these meetings are going away, so there is nothing left to re-index for them
        db.execute(f"DELETE FROM rag_dirty WHERE meeting_id IN (SELECT id FROM meetings WHERE owner_email = ? "
                   f"AND id IN ({','.join('?' * len(meeting_ids))}))", [owner_email, *meeting_ids])
    return len(ids)


def _transcript_rows(meeting):
    header = f"Meeting: {meeting['title']} ({meeting['meeting_date']})"
    return [{
        "kind": "transcript",
        "speaker": ", ".join(chunk.speakers),
        "text": chunk.text,
        "embed_text": f"{header}\n{chunk.text}",  # the header gives the chunk its context; it isn't stored
        "turn_start": chunk.turn_start,
        "turn_end": chunk.turn_end,
    } for chunk in chunk_transcript(meeting["transcript_excerpt"] or "")]


def _commitment_rows(db, meeting):
    """One searchable chunk per commitment, so questions like "who owns the retest?" match it directly."""
    header = f"Meeting: {meeting['title']} ({meeting['meeting_date']})"
    rows = []
    for c in db.execute(
        "SELECT task_text, owner_name, due_date, status, source_quote FROM commitments WHERE meeting_id = ? ORDER BY id",
        (meeting["id"],),
    ):
        text = (f"Commitment: {c['task_text']}\nOwner: {c['owner_name']}\n"
                f"Due: {c['due_date'] or 'no due date'}\nStatus: {(c['status'] or 'pending').replace('_', ' ')}")
        if c["source_quote"] and c["source_quote"] != c["task_text"]:
            text += f"\nSaid in the meeting: {c['source_quote']}"
        rows.append({"kind": "commitment", "speaker": c["owner_name"], "text": text,
                     "embed_text": f"{header}\n{text}", "turn_start": None, "turn_end": None})
    return rows


def _embed(rows, embed_fn, model_name):
    if embed_fn is None:
        from . import llm  # imported here: litellm is slow to load and only indexing needs it
        embed_fn, model_name = llm.embed, model_name or llm.EMBEDDING_MODEL_NAME
    vectors = embed_fn([row["embed_text"] for row in rows])
    if len(vectors) != len(rows):
        raise RagError(f"The embedding step returned {len(vectors)} vectors for {len(rows)} chunks.")
    return vectors, model_name or "unknown"


def _replace_rows(db, meeting, rows, vectors, model_name):
    delete_index(db, meeting["owner_email"], [meeting["id"]], clear_dirty=False)
    for row, vector in zip(rows, vectors):
        chunk_id = db.execute(
            "INSERT INTO rag_chunks (meeting_id, owner_email, kind, speaker, text, meeting_title, meeting_date, turn_start, turn_end) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (meeting["id"], meeting["owner_email"], row["kind"], row["speaker"], row["text"],
             meeting["title"], meeting["meeting_date"], row["turn_start"], row["turn_end"]),
        ).lastrowid
        db.execute("INSERT INTO rag_embeddings (chunk_id, model, dim, vector) VALUES (?, ?, ?, ?)",
                   (chunk_id, model_name, len(vector), np.asarray(vector, dtype=np.float32).tobytes()))
        db.execute("INSERT INTO rag_fts (rowid, chunk_id, text) VALUES (?, ?, ?)", (chunk_id, chunk_id, row["text"]))


def index_meeting(db, meeting_id, embed_fn=None, model_name=None) -> int:
    """(Re)builds one meeting's index and commits. Returns the number of chunks stored.
    embed_fn(list[str]) -> list of vectors defaults to the app's embedding model; tests pass a stub.
    Raises MeetingNotFound if the meeting is gone."""
    ensure_schema(db)
    # Changes marked before this moment are picked up by the read below; later ones stay marked.
    started = db.execute("SELECT strftime('%Y-%m-%d %H:%M:%f', 'now')").fetchone()[0]
    meeting = db.execute(
        "SELECT id, owner_email, title, meeting_date, transcript_excerpt FROM meetings WHERE id = ?", (meeting_id,)
    ).fetchone()
    if meeting is None:
        raise MeetingNotFound(f"Meeting {meeting_id} no longer exists.")
    rows = _transcript_rows(meeting) + _commitment_rows(db, meeting)
    vectors, model_name = _embed(rows, embed_fn, model_name) if rows else ([], model_name)
    try:
        _replace_rows(db, meeting, rows, vectors, model_name)
        db.execute("DELETE FROM rag_dirty WHERE meeting_id = ? AND marked_at <= ?", (meeting_id, started))
        db.commit()
    except Exception:
        db.rollback()
        raise
    return len(rows)


def mark_unindexed(db) -> int:
    """Marks every meeting that has something searchable but no chunks yet, so a database from before
    indexing existed (or one where indexing was off) catches up by itself. Returns how many."""
    return db.execute(
        "INSERT OR IGNORE INTO rag_dirty (meeting_id, marked_at) "
        "SELECT m.id, strftime('%Y-%m-%d %H:%M:%f', 'now') FROM meetings m "
        "WHERE (COALESCE(m.transcript_excerpt, '') != '' OR EXISTS (SELECT 1 FROM commitments c WHERE c.meeting_id = m.id)) "
        "AND NOT EXISTS (SELECT 1 FROM rag_chunks r WHERE r.meeting_id = m.id)"
    ).rowcount


def prepare(db) -> int:
    """Creates the RAG tables and triggers if needed and marks never-indexed meetings. Commits."""
    ensure_schema(db)
    marked = mark_unindexed(db)
    db.commit()
    return marked


def sweep_dirty(db, enqueue, quiet_seconds=QUIET_SECONDS) -> int:
    """Calls enqueue(db, owner_email, meeting_id) once for each meeting that changed and then stayed
    quiet for quiet_seconds, so a burst of edits becomes one re-index. Marks for meetings that no
    longer exist are dropped. A meeting changed again while this runs keeps its newer mark.
    Commits. Returns how many re-indexes were queued."""
    if not has_table(db, "rag_dirty"):
        return 0
    cutoff = db.execute("SELECT strftime('%Y-%m-%d %H:%M:%f', 'now', ?)", (f"-{quiet_seconds} seconds",)).fetchone()[0]
    due = db.execute(
        "SELECT d.meeting_id, d.marked_at, m.owner_email FROM rag_dirty d "
        "LEFT JOIN meetings m ON m.id = d.meeting_id WHERE d.marked_at <= ?", (cutoff,)
    ).fetchall()
    queued = 0
    try:
        for row in due:
            if row["owner_email"] is not None:
                enqueue(db, row["owner_email"], row["meeting_id"])
                queued += 1
            db.execute("DELETE FROM rag_dirty WHERE meeting_id = ? AND marked_at = ?", (row["meeting_id"], row["marked_at"]))
        db.commit()
    except Exception:
        db.rollback()
        raise
    return queued
