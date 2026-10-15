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


def use_test_secret_key():
    """Points secret_store at a throwaway key, so tests never create or read
    the real secret.key."""
    import os
    import secret_store
    from cryptography.fernet import Fernet
    if not os.environ.get("COMMITMENT_TRACKER_SECRET_KEY"):
        os.environ["COMMITMENT_TRACKER_SECRET_KEY"] = Fernet.generate_key().decode()
    secret_store.reset()


use_test_secret_key()  # every suite imports this module before app

# Run background jobs synchronously inside the request, so a response (e.g.
# an upload's) already carries the extraction result, as before 0.9.0.
import os as _os  # noqa: E402
_os.environ.setdefault("CT_JOBS_INLINE", "1")

TEST_PASSWORD = "test-password-123"


def login(client, email="t@example.com", password=TEST_PASSWORD):
    """Signs a test client in (creating the account on first use) and makes
    it send the CSRF header on every request, like static/app.js does."""
    client.environ_base["HTTP_X_CT_REQUEST"] = "1"
    r = client.post("/api/auth/signup", json={"email": email, "password": password})
    if r.status_code == 409:
        r = client.post("/api/auth/login", json={"email": email, "password": password})
    assert r.status_code in (200, 201), r.get_json()
    return client


def webhook_setup(client):
    """(paths, signing secret) of the signed-in account's inbound webhooks."""
    info = client.get("/api/webhook-security").get_json()
    return info["paths"], client.post("/api/webhook-security/reveal").get_json()["secret"]


def signed_post(client, path, payload, secret, timestamp=None, delivery=None):
    """POSTs JSON signed the way an external sender must (webhook_security.py)."""
    import json
    import time
    import webhook_security
    body = json.dumps(payload)
    ts = str(int(timestamp if timestamp is not None else time.time()))
    headers = {"X-CT-Timestamp": ts, "X-CT-Signature": webhook_security.sign(secret, ts, body)}
    if delivery:
        headers["X-CT-Delivery"] = delivery
    return client.post(path, data=body, headers=headers, content_type="application/json")


def fresh_app(email="t@example.com"):
    """Imports the app against a brand-new temporary database (never the real
    commitment_tracker.db) and returns (app_module, logged-in test client, db_path)."""
    import app as appmod
    db_path = Path(tempfile.mkdtemp()) / "test.db"
    appmod.DB_PATH = db_path
    appmod.init_db()
    appmod.app.testing = True
    client = login(appmod.app.test_client(), email)
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
