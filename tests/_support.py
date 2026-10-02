"""Shared helpers for the test scripts: project import path, a throwaway
database, PASS/FAIL reporting, and a fake Google Sheets API."""

import atexit
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

_failures = []


def ok(condition, message):
    print(("PASS " if condition else "FAIL ") + message)
    if not condition:
        _failures.append(message)


@atexit.register
def _exit_status():
    if _failures:
        print(f"\n{len(_failures)} check(s) failed")
        sys.stdout.flush()
        import os
        os._exit(1)


def fresh_app(email="t@example.com"):
    """Imports the app against a brand-new temporary database (never the real
    commitment_tracker.db) and returns (app_module, logged-in test client, db_path)."""
    import app as appmod
    db_path = Path(tempfile.mkdtemp()) / "test.db"
    appmod.DB_PATH = db_path
    appmod.init_db()
    client = appmod.app.test_client()
    client.post("/api/identify", json={"email": email})
    return appmod, client, db_path


class FakeSheet:
    """Stand-in for the Google Sheets API: a {(row, column): value} grid.
    install() patches googleapiclient so google_sheets.py talks to it."""

    def __init__(self, latency=0.0):
        self.grid, self.calls, self.latency = {}, [], latency

    def install(self):
        import googleapiclient.discovery
        import google_sheets
        sheet = self

        class Exec:
            def __init__(self, fn): self.fn = fn
            def execute(self): return self.fn()

        class Values:
            def get(self, spreadsheetId, **kw):
                rows = [r for (r, c) in sheet.grid if c == "A"]
                n = max(rows) if rows else 0
                return Exec(lambda: {"values": [[sheet.grid.get((r, "A"), "")] for r in range(1, n + 1)]} if n else {})

            def update(self, spreadsheetId, valueInputOption, body, **kw):
                rng = kw["range"]
                sheet.calls.append(rng)
                start = rng.split(":")[0]
                col0, row0 = start[0], int(start[1:])

                def go():
                    if sheet.latency:
                        import time
                        time.sleep(sheet.latency)
                    for ri, vals in enumerate(body["values"]):
                        for ci, v in enumerate(vals):
                            sheet.grid[(row0 + ri, chr(ord(col0) + ci))] = v
                    return {}
                return Exec(go)

        class Service:
            def spreadsheets(self): return self
            def values(self): return Values()

        googleapiclient.discovery.build = lambda *a, **k: Service()
        google_sheets.get_credentials = lambda: None
        return self

    def row(self, n, cols="ABCDE"):
        return [self.grid.get((n, c), "") for c in cols]
