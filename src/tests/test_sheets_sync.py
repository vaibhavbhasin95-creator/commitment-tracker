"""Google Sheets sync (0.3.1 / 0.4.0): row alignment, double-click race,
status synonyms, auto-sync, bulk sync, status push, no bounce-back.
Uses a fake Sheets API — never touches a real spreadsheet."""

import threading

from _support import login, FakeSheet, fresh_app, ok, signed_post, webhook_setup

sheet = FakeSheet(latency=0.3).install()
appmod, c, _ = fresh_app()
paths, secret = webhook_setup(c)

c.post("/api/tracker-settings", json={"provider": "google_sheets", "config": {"spreadsheet_id": "x", "auto_sync": False}, "enabled": True})
mid = c.post("/api/meetings", json={"title": "m"}).get_json()["id"]
ids = [c.post("/api/commitments", json={"meeting_id": mid, "task_text": t, "owner_name": "Bhuvan", "due_date": d}).get_json()["id"]
       for t, d in [("no due date task", None), ("second task", "2026-09-30"), ("third task", None)]]
ok(sheet.grid == {}, "auto-sync off: adding tasks writes nothing to the sheet")

refs = [c.post(f"/api/commitments/{i}/sync-tracker").get_json()["external_ref"] for i in ids]
ok(refs == ["sheet-1", "sheet-2", "sheet-3"], f"rows get sequential refs {refs}")
ok(sheet.row(1, "ABCDEF") == ["ID", "Task", "Owner", "Due date", "Status", "Owner email"], "header row written to an empty sheet")
ok(sheet.row(2) == [1, "no due date task", "Bhuvan", "", "Pending"], "blank due date does not shift columns (0.3.1 bug)")
ok(all((r, col) not in sheet.grid for r in range(1, 5) for col in "GHI"), "nothing written beyond column F")

race_id = c.post("/api/commitments", json={"meeting_id": mid, "task_text": "race task", "owner_name": "B"}).get_json()["id"]


def fire():
    other = appmod.app.test_client()
    login(other, "t@example.com")
    other.post(f"/api/commitments/{race_id}/sync-tracker")


threads = [threading.Thread(target=fire) for _ in range(2)]
[t.start() for t in threads]
[t.join() for t in threads]
ok(sum(1 for v in sheet.grid.values() if v == "race task") == 1, "two simultaneous syncs write exactly one row")

for typed, expected in [("Closed", "done"), ("In Progress", "in_progress"), ("open", "pending")]:
    r = signed_post(c, paths["tracker"], {"external_ref": "sheet-1", "status": typed, "provider": "google_sheets"}, secret)
    ok(r.status_code == 201 and f"'{expected}'" in r.get_json()["detail"], f"status word {typed!r} -> {expected}")
ok(signed_post(c, paths["tracker"], {"external_ref": "sheet-1", "status": "bogus"}, secret).status_code == 400,
   "unknown status word rejected")
other_client = login(appmod.app.test_client(), "other@example.com")
other_paths, other_secret = webhook_setup(other_client)
ok(signed_post(other_client, other_paths["tracker"], {"external_ref": "sheet-1", "status": "done"}, other_secret).status_code == 404,
   "webhook cannot touch another account's task")
ok(signed_post(c, other_paths["tracker"], {"external_ref": "sheet-1", "status": "done"}, secret).status_code == 401,
   "signing with one account's secret doesn't work on another account's URL")

sheet.calls.clear()
r = c.post("/api/tracker/sync-all").get_json()
ok(r == {"ok": True, "synced": 0, "readded": 0}, "sync-all with nothing pending writes nothing")

c.post("/api/tracker-settings", json={"provider": "google_sheets", "config": {"spreadsheet_id": "x", "auto_sync": True}, "enabled": True})
new = c.post("/api/commitments", json={"meeting_id": mid, "task_text": "auto task", "owner_name": "Sarah"}).get_json()
ok(new["synced_to_tracker"] == 1, "auto-sync on: new task written immediately")
c.patch(f"/api/commitments/{new['id']}", json={"status": "done"})
ok("Done" in [v for (r, col), v in sheet.grid.items() if col == "E"], "in-app status change updates column E")

sheet.calls.clear()
signed_post(c, paths["tracker"], {"external_ref": "sheet-2", "status": "Done", "provider": "google_sheets"}, secret)
ok(sheet.calls == [], "update coming FROM the sheet is not written back to it when the sheet already matches")
signed_post(c, paths["tracker"], {"external_ref": "sheet-2", "status": "Closed", "provider": "google_sheets"}, secret)
ok(sheet.calls == ["B3:F3"] and sheet.grid[(3, "E")] == "Done",
   "a status typed as 'Closed' is tidied to 'Done' in the sheet (API writes don't fire On edit, so no loop)")
