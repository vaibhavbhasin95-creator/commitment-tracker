"""One-off: index every meeting that already exists, so old meetings are searchable (Step 1.3).

    python src/rag/backfill.py                      # every account's meetings
    python src/rag/backfill.py --email you@example.com
    python src/rag/backfill.py --db path/to/other.db

Safe to repeat: each meeting's index is replaced, never duplicated. Prints meeting ids and chunk
counts only, never transcript text.
"""

import argparse
import sqlite3
import sys
from pathlib import Path

SRC = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SRC))

from rag.errors import RagError  # noqa: E402
from rag.indexer import index_meeting  # noqa: E402


def meeting_ids(db, email=None):
    sql, params = "SELECT id FROM meetings WHERE transcript_excerpt IS NOT NULL AND transcript_excerpt != ''", ()
    if email:
        sql, params = sql + " AND owner_email = ?", (email,)
    return [row["id"] for row in db.execute(sql + " ORDER BY id", params)]


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--email", help="only this account's meetings")
    parser.add_argument("--db", default=str(SRC.parent / "commitment_tracker.db"), help="database file")
    args = parser.parse_args()

    db = sqlite3.connect(args.db, timeout=30)
    db.row_factory = sqlite3.Row
    ids = meeting_ids(db, args.email)
    print(f"Indexing {len(ids)} meeting(s) from {args.db}")
    failed = 0
    for meeting_id in ids:
        try:
            print(f"  meeting {meeting_id}: {index_meeting(db, meeting_id)} chunks")
        except RagError as e:
            failed += 1
            print(f"  meeting {meeting_id}: FAILED, {e}")
    db.close()
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
