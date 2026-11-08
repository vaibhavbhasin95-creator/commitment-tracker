"""Database tables for RAG (Step 1.1): searchable chunks, their vectors, a keyword index and a query log.
All live in the app's own SQLite database, next to meetings and commitments."""

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS rag_chunks(
    id INTEGER PRIMARY KEY AUTOINCREMENT,   -- AUTOINCREMENT: an id is never reused, so ids saved in rag_queries.chunk_ids stay unambiguous
    meeting_id INTEGER NOT NULL,
    owner_email TEXT NOT NULL,
    kind TEXT NOT NULL CHECK(kind IN ('transcript','commitment')),
    speaker TEXT,
    text TEXT NOT NULL,              -- the chunk itself, read back for the prompt and citations
    meeting_title TEXT,
    meeting_date TEXT,
    turn_start INTEGER,
    turn_end INTEGER
);

CREATE TABLE IF NOT EXISTS rag_embeddings(
    chunk_id INTEGER PRIMARY KEY,
    model TEXT NOT NULL,             -- the embedding model that made the vector; vectors from different models can't be compared
    dim INTEGER NOT NULL,
    vector BLOB NOT NULL             -- float32 bytes: numpy array .tobytes() / numpy.frombuffer
);

CREATE VIRTUAL TABLE IF NOT EXISTS rag_fts USING fts5(
    chunk_id UNINDEXED,
    text
);

CREATE TABLE IF NOT EXISTS rag_queries(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_email TEXT NOT NULL,
    question TEXT NOT NULL,
    answer TEXT,
    chunk_ids TEXT,                  -- JSON list of the chunk ids cited, e.g. "[12, 40, 41]"
    model TEXT,
    created_at TEXT NOT NULL
);

-- Meetings whose index is out of date. The triggers below add a row whenever something searchable
-- about a meeting changes; marked_at is refreshed on each change so a burst of edits counts as one.
CREATE TABLE IF NOT EXISTS rag_dirty(
    meeting_id INTEGER PRIMARY KEY,
    marked_at TEXT NOT NULL          -- 'YYYY-MM-DD HH:MM:SS.fff' UTC, comparable as text
);

CREATE INDEX IF NOT EXISTS idx_rag_chunks_owner ON rag_chunks(owner_email);
CREATE INDEX IF NOT EXISTS idx_rag_chunks_meetingid ON rag_chunks(meeting_id);
"""

# Triggers on the app's own tables (meetings, commitments). They only write to rag_dirty and change
# nothing else; drop_triggers() removes them. A change to a column that isn't searchable (an owner's
# email, a sync reference, updated_at) does not fire them.
_MARK = "INSERT OR REPLACE INTO rag_dirty (meeting_id, marked_at) VALUES ({id}, strftime('%Y-%m-%d %H:%M:%f', 'now'));"
TRIGGER_NAMES = ("rag_meeting_added", "rag_meeting_changed", "rag_commitment_added", "rag_commitment_changed", "rag_commitment_removed")
TRIGGER_SQL = f"""
CREATE TRIGGER IF NOT EXISTS rag_meeting_added AFTER INSERT ON meetings
BEGIN {_MARK.format(id="NEW.id")} END;

CREATE TRIGGER IF NOT EXISTS rag_meeting_changed AFTER UPDATE ON meetings
WHEN OLD.title IS NOT NEW.title OR OLD.meeting_date IS NOT NEW.meeting_date
  OR OLD.transcript_excerpt IS NOT NEW.transcript_excerpt
BEGIN {_MARK.format(id="NEW.id")} END;

CREATE TRIGGER IF NOT EXISTS rag_commitment_added AFTER INSERT ON commitments
BEGIN {_MARK.format(id="NEW.meeting_id")} END;

CREATE TRIGGER IF NOT EXISTS rag_commitment_changed AFTER UPDATE ON commitments
WHEN OLD.task_text IS NOT NEW.task_text OR OLD.owner_name IS NOT NEW.owner_name
  OR OLD.due_date IS NOT NEW.due_date OR OLD.status IS NOT NEW.status
  OR OLD.source_quote IS NOT NEW.source_quote
BEGIN {_MARK.format(id="NEW.meeting_id")} END;

CREATE TRIGGER IF NOT EXISTS rag_commitment_removed AFTER DELETE ON commitments
BEGIN {_MARK.format(id="OLD.meeting_id")} END;
"""


def has_table(db, name):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)).fetchone() is not None


def ensure_schema(db):
    """Creates the RAG tables, and the triggers that keep the index fresh, on first use. Safe to call
    repeatedly; no existing table is altered. The triggers are skipped on a database that doesn't have
    the app's meetings and commitments tables."""
    db.executescript(SCHEMA_SQL)
    if has_table(db, "meetings") and has_table(db, "commitments"):
        db.executescript(TRIGGER_SQL)


def drop_triggers(db):
    """Removes the triggers (to revert the feature). The RAG tables stay."""
    for name in TRIGGER_NAMES:
        db.execute(f"DROP TRIGGER IF EXISTS {name}")
    db.commit()