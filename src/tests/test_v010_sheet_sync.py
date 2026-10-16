"""0.10.0: every field syncs both ways between the app and the Google Sheet
(task, owner, owner email, due date, status), and the most recent edit wins.
Fake Sheets API — never touches a real spreadsheet."""

import sqlite3
import time

from _support import FakeSheet, fresh_app, ok, signed_post, webhook_setup

sheet = FakeSheet().install()
appmod, c, tmp = fresh_app("owner@corp.com")
paths, secret = webhook_setup(c)
raw = sqlite3.connect(tmp)

c.post("/api/people", json={"first_name": "Kevin", "last_name": "Heys", "email": "kevin@corp.com"})
c.post("/api/people", json={"first_name": "Sarah", "last_name": "Xian", "email": "sarah@corp.com"})
c.post("/api/tracker-settings", json={"provider": "google_sheets", "config": {"spreadsheet_id": "x", "auto_sync": True}})
mid = c.post("/api/meetings", json={"title": "Planning"}).get_json()["id"]
cid = c.post("/api/commitments", json={"meeting_id": mid, "task_text": "Send the budget", "owner_name": "Kevin",
                                      "due_date": "2026-11-01"}).get_json()["id"]
row = lambda n=2: sheet.row(n, "ABCDEF")
ok(row(1) == ["ID", "Task", "Owner", "Due date", "Status", "Owner email"], "1a header has the Owner email column (F)")
ok(row() == [1, "Send the budget", "Kevin", "2026-11-01", "Pending", "kevin@corp.com"], f"1b new row carries the owner email ({row()})")

# ---- app -> sheet: every field ----
r = c.patch(f"/api/commitments/{cid}", json={"task_text": "Send the revised budget", "due_date": "2026-11-05"}).get_json()
ok(r["changed"] == ["task_text", "due_date"] and row()[1:4] == ["Send the revised budget", "Kevin", "2026-11-05"],
   "2a task + due date edited in the app rewrite the sheet row")
c.patch(f"/api/commitments/{cid}", json={"owner_name": "Sarah"})
ok(row()[2] == "Sarah" and row()[5] == "sarah@corp.com", "2b new owner in the app -> matched email, both written to the sheet")
c.patch(f"/api/commitments/{cid}", json={"owner_email": "boss@corp.com"})
ok(row()[5] == "boss@corp.com", "2c owner email set by hand reaches column F")
ok(c.patch(f"/api/commitments/{cid}", json={"due_date": "31/10/2026"}).status_code == 400, "2d bad date refused")
ok(c.patch(f"/api/commitments/{cid}", json={"task_text": "  "}).status_code == 400, "2e blank task refused")
c.patch(f"/api/commitments/{cid}", json={"due_date": ""})
ok(row()[3] == "" and c.get(f"/api/commitments/{cid}").get_json()["due_date"] is None, "2f due date cleared on both sides")

# ---- sheet -> app: every field ----
raw.execute("UPDATE commitments SET updated_at = '2000-01-01T00:00:00' WHERE id = ?", (cid,)); raw.commit()
r = signed_post(c, paths["tracker"], {"external_ref": "sheet-1", "task": "Send budget v3", "owner": "Kevin Heys",
                                      "due_date": "2026-12-01", "status": "In progress", "owner_email": "boss@corp.com",
                                      "provider": "google_sheets"}, secret)
got = c.get(f"/api/commitments/{cid}").get_json()
ok(r.status_code == 201 and r.get_json()["applied"] and got["task_text"] == "Send budget v3" and got["due_date"] == "2026-12-01"
   and got["status"] == "in_progress" and got["owner_name"] == "Kevin Heys", f"3a task/owner/due/status from the sheet applied ({r.get_json()['detail']})")
ok(got["owner_email"] == "boss@corp.com", "3b a hand-set email the sheet didn't change stays")
raw.execute("UPDATE commitments SET updated_at = '2000-01-01T00:00:00', owner_match = 'auto' WHERE id = ?", (cid,)); raw.commit()
sheet.calls.clear()
signed_post(c, paths["tracker"], {"external_ref": "sheet-1", "owner": "Sarah", "owner_email": "boss@corp.com",
                                  "provider": "google_sheets"}, secret)
got = c.get(f"/api/commitments/{cid}").get_json()
ok(got["owner_email"] == "sarah@corp.com" and row()[5] == "sarah@corp.com",
   "3c owner renamed in the sheet (email untouched) -> matched from People, and the email is filled into the sheet")
raw.execute("UPDATE commitments SET updated_at = '2000-01-01T00:00:00' WHERE id = ?", (cid,)); raw.commit()
signed_post(c, paths["tracker"], {"external_ref": "sheet-1", "owner_email": "new.person@corp.com", "provider": "google_sheets"}, secret)
got = c.get(f"/api/commitments/{cid}").get_json()
ok(got["owner_email"] == "new.person@corp.com" and got["owner_match"] == "manual", "3d email typed in the sheet = manual pick")
r = signed_post(c, paths["tracker"], {"external_ref": "sheet-1", "due_date": "next friday", "provider": "google_sheets"}, secret)
ok(r.status_code == 400 and "YYYY-MM-DD" in r.get_json()["error"], "3e unreadable date from the sheet refused with a reason")
r = signed_post(c, paths["tracker"], {"external_ref": "sheet-1", "owner_email": "not-an-email", "provider": "google_sheets"}, secret)
ok(r.status_code == 400, "3f invalid email from the sheet refused")
r = signed_post(c, paths["tracker"], {"external_ref": "sheet-1", "provider": "google_sheets"}, secret)
ok(r.status_code == 400, "3g empty update refused")

# ---- most recent edit wins ----
c.patch(f"/api/commitments/{cid}", json={"task_text": "App wins"})          # app edit: now
sheet.calls.clear()
old_edit = int(time.time()) - 120
r = signed_post(c, paths["tracker"], {"external_ref": "sheet-1", "task": "Older sheet edit", "edited_at": old_edit,
                                      "provider": "google_sheets"}, secret)
body = r.get_json()
ok(r.status_code == 201 and body["applied"] is False and "newer" in body["detail"], "4a an edit older than the app's last change is ignored")
ok(c.get(f"/api/commitments/{cid}").get_json()["task_text"] == "App wins" and row()[1] == "App wins" and sheet.calls == ["B2:F2"],
   "4b ...and the sheet row is put back to the app's (newer) version")
r = signed_post(c, paths["tracker"], {"external_ref": "sheet-1", "task": "Newer sheet edit", "edited_at": int(time.time()) + 1,
                                      "provider": "google_sheets"}, secret)
ok(r.get_json()["applied"] and c.get(f"/api/commitments/{cid}").get_json()["task_text"] == "Newer sheet edit",
   "4c a newer sheet edit wins over the app")
time.sleep(1.1)
c.patch(f"/api/commitments/{cid}", json={"task_text": "Then the app again"})
ok(row()[1] == "Then the app again", "4d ...and a later app edit wins back, written to the sheet")
log = [t["detail"] for t in c.get(f"/api/commitments/{cid}").get_json()["tracker_history"]]
ok(any("Ignored an edit" in d for d in log) and any(d.startswith("Updated task via") for d in log), "4e both outcomes are in the task's sync history")

# ---- a sheet made before 0.10.0 gets the new header ----
legacy = FakeSheet().install()
legacy.grid.update({(1, "A"): "ID", (1, "B"): "Task", (1, "C"): "Owner", (1, "D"): "Due date", (1, "E"): "Status"})
c.post("/api/commitments", json={"meeting_id": mid, "task_text": "Second", "owner_name": "Kevin"})
ok(legacy.row(1, "ABCDEF")[5] == "Owner email" and legacy.row(2, "ABCDEF")[5] == "kevin@corp.com",
   "5a an older 5-column sheet gains the Owner email header; Status stays in column E")

# ---- 0.10.2: column A numbers from 1; rows are found by that number ----
def delete_row(fake, n):
    """Simulates deleting sheet row n: everything below moves up one."""
    fake.grid = {((r - 1 if r > n else r), col): v for (r, col), v in fake.grid.items() if r != n}

fresh = FakeSheet().install()
mid2 = c.post("/api/meetings", json={"title": "Fresh sheet"}).get_json()["id"]
ids = [c.post("/api/commitments", json={"meeting_id": mid2, "task_text": f"Task {i}", "owner_name": "Kevin"}).get_json()["id"]
       for i in range(1, 4)]
ok([fresh.grid[(r, "A")] for r in (2, 3, 4)] == [1, 2, 3], "6a an empty sheet numbers tasks 1, 2, 3 (not the app's internal ids)")
refs = [c.get(f"/api/commitments/{i}").get_json()["external_ref"] for i in ids]
ok(refs == ["sheet-1", "sheet-2", "sheet-3"], f"6b tasks are linked by that number ({refs})")

delete_row(fresh, 2)                     # someone deletes task 1's row; task 2 and 3 move up
c.patch(f"/api/commitments/{ids[2]}", json={"task_text": "Task 3 edited"})
ok(fresh.row(3, "AB") == [3, "Task 3 edited"] and fresh.row(2, "AB") == [2, "Task 2"],
   "6c after a row is deleted, an edit still lands on the right task's row")
c.patch(f"/api/commitments/{ids[0]}", json={"task_text": "Task 1 edited"})
got = c.get(f"/api/commitments/{ids[0]}").get_json()
ok(got["external_ref"] is None and "Task 1 edited" not in fresh.grid.values(),
   "6d a task whose row was deleted is unlinked — nothing is written into another task's row")
ok(any("no longer in the sheet" in t["detail"] for t in got["tracker_history"]), "6e ...and the sync history says so")
r = c.post(f"/api/commitments/{ids[0]}/sync-tracker").get_json()
ok(r["external_ref"] == "sheet-4" and fresh.row(4, "AB") == [4, "Task 1 edited"], "6f Sync to tracker adds it back as a new row, numbered 4")

r = signed_post(c, paths["tracker"], {"external_ref": "sheet-3", "row_ref": "row-3", "status": "Done",
                                      "provider": "google_sheets"}, secret)
ok(r.status_code == 201 and c.get(f"/api/commitments/{ids[2]}").get_json()["status"] == "done",
   "6g an edit from the sheet is matched by its column-A number")

cleared = FakeSheet().install()          # the whole sheet emptied (header too)
mid3 = c.post("/api/meetings", json={"title": "After clearing"}).get_json()["id"]
c.post("/api/commitments", json={"meeting_id": mid3, "task_text": "First again", "owner_name": "Kevin"})
ok(cleared.row(1, "A") == ["ID"] and cleared.row(2, "AB") == [1, "First again"], "6h a cleared sheet starts again from 1")

raw.execute("UPDATE commitments SET external_ref = 'row-2' WHERE id = ?", (ids[1],)); raw.commit()
r = signed_post(c, paths["tracker"], {"external_ref": "sheet-99", "row_ref": "row-2", "status": "In progress",
                                      "provider": "google_sheets"}, secret)
ok(r.status_code == 201 and c.get(f"/api/commitments/{ids[1]}").get_json()["status"] == "in_progress",
   "6i a row synced before 0.10.2 (row-<n>) is still matched through row_ref")

emptied = FakeSheet().install()          # user deletes every row, then presses "Sync all to sheet"
total = len(c.get("/api/commitments").get_json())
r = c.post("/api/tracker/sync-all").get_json()
numbers = [emptied.grid[(row_n, "A")] for row_n in range(2, total + 2)]
ok(r["synced"] == total and r["readded"] > 0 and numbers == list(range(1, total + 1)),
   f"6j emptying the sheet then 'Sync all' rebuilds it numbered 1..{total} ({r})")
