"""Google Sheets sync (0.3.1 / 0.4.0): row alignment, double-click race,
status synonyms, auto-sync, bulk sync, status push, no bounce-back.
Uses a fake Sheets API — never touches a real spreadsheet."""

import threading

from _support import FakeSheet, fresh_app, ok

sheet = FakeSheet(latency=0.3).install()
appmod, c, _ = fresh_app()

c.post("/api/tracker-settings", json={"provider": "google_sheets", "config": {"spreadsheet_id": "x", "auto_sync": False}, "enabled": True})
mid = c.post("/api/meetings", json={"title": "m"}).get_json()["id"]
ids = [c.post("/api/commitments", json={"meeting_id": mid, "task_text": t, "owner_name": "Bhuvan", "due_date": d}).get_json()["id"]
       for t, d in [("no due date task", None), ("second task", "2026-09-30"), ("third task", None)]]
ok(sheet.grid == {}, "auto-sync off: adding tasks writes nothing to the sheet")

refs = [c.post(f"/api/commitments/{i}/sync-tracker").get_json()["external_ref"] for i in ids]
ok(refs == ["row-2", "row-3", "row-4"], f"rows get sequential refs {refs}")
ok(sheet.row(1) == ["ID", "Task", "Owner", "Due date", "Status"], "header row written to an empty sheet")
ok(sheet.row(2) == [ids[0], "no due date task", "Bhuvan", "", "Pending"], "blank due date does not shift columns (0.3.1 bug)")
ok(all((r, col) not in sheet.grid for r in range(1, 5) for col in "FGHI"), "nothing written beyond column E")

race_id = c.post("/api/commitments", json={"meeting_id": mid, "task_text": "race task", "owner_name": "B"}).get_json()["id"]


def fire():
    other = appmod.app.test_client()
    other.post("/api/identify", json={"email": "t@example.com"})
    other.post(f"/api/commitments/{race_id}/sync-tracker")


threads = [threading.Thread(target=fire) for _ in range(2)]
[t.start() for t in threads]
[t.join() for t in threads]
ok(sum(1 for v in sheet.grid.values() if v == "race task") == 1, "two simultaneous syncs write exactly one row")

for typed, expected in [("Closed", "done"), ("In Progress", "in_progress"), ("open", "pending")]:
    r = c.post("/api/webhooks/tracker-update", json={"email": "t@example.com", "external_ref": "row-2", "status": typed, "provider": "google_sheets"})
    ok(r.status_code == 201 and f"'{expected}'" in r.get_json()["detail"], f"status word {typed!r} -> {expected}")
ok(c.post("/api/webhooks/tracker-update", json={"email": "t@example.com", "external_ref": "row-2", "status": "bogus"}).status_code == 400,
   "unknown status word rejected")
ok(c.post("/api/webhooks/tracker-update", json={"email": "other@example.com", "external_ref": "row-2", "status": "done"}).status_code == 404,
   "webhook cannot touch another account's task")

sheet.calls.clear()
r = c.post("/api/tracker/sync-all").get_json()
ok(r == {"ok": True, "synced": 0}, "sync-all with nothing pending writes nothing")

c.post("/api/tracker-settings", json={"provider": "google_sheets", "config": {"spreadsheet_id": "x", "auto_sync": True}, "enabled": True})
new = c.post("/api/commitments", json={"meeting_id": mid, "task_text": "auto task", "owner_name": "Sarah"}).get_json()
ok(new["synced_to_tracker"] == 1, "auto-sync on: new task written immediately")
c.patch(f"/api/commitments/{new['id']}", json={"status": "done"})
ok("Done" in [v for (r, col), v in sheet.grid.items() if col == "E"], "in-app status change updates column E")

sheet.calls.clear()
c.post("/api/webhooks/tracker-update", json={"email": "t@example.com", "external_ref": "row-3", "status": "Closed", "provider": "google_sheets"})
ok(sheet.calls == [], "update coming FROM the sheet is not written back to it (no loop)")
