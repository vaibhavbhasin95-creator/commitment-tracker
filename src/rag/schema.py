"""Database tables for RAG (Step 1.1): searchable chunks, their vectors, a keyword index and a query log.
All live in the app's own SQLite database, next to meetings and commitments."""

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS rag_chunks(
    id INTEGER PRIMARY KEY,
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

CREATE INDEX IF NOT EXISTS idx_rag_chunks_owner ON rag_chunks(owner_email);
CREATE INDEX IF NOT EXISTS idx_rag_chunks_meetingid ON rag_chunks(meeting_id);
"""


def ensure_schema(db):
    """Creates the RAG tables on first use. Safe to call repeatedly; no existing table is altered."""
    db.executescript(SCHEMA_SQL)