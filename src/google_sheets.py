"""
Real Google Sheets API integration for the outbound half of task-tracker
sync (app.py's sync_tracker(), provider == "google_sheets").

Uses a one-time local OAuth consent flow (see google_oauth_setup.py) rather
than a service account: this is a personal Google account with no Workspace
admin to provision a service account through, so the practical path is an
OAuth Client ID (Desktop app type) plus one interactive browser consent,
after which the refresh token in google_token.json lets every future call
run non-interactively.

Run once, before the first real sync:
    python google_oauth_setup.py
"""

import json
import os
from pathlib import Path

import secret_store

BASE_DIR = Path(__file__).resolve().parent.parent   # project root — credential files live there, not in src/
TOKEN_PATH = BASE_DIR / "google_token.json"
CLIENT_SECRET_GLOB = "client_secret_*.json"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


class GoogleAuthNotReadyError(RuntimeError):
    """Raised when google_token.json doesn't exist yet, or no client-secret
    file is present — run `python google_oauth_setup.py` first."""


def find_client_secret_path():
    """CT_GOOGLE_CLIENT_SECRET_FILE wins, so the client secret can live outside
    the project folder; otherwise the first client_secret_*.json in the root."""
    env_path = os.environ.get("CT_GOOGLE_CLIENT_SECRET_FILE", "").strip()
    if env_path:
        if not Path(env_path).is_file():
            raise GoogleAuthNotReadyError(f"CT_GOOGLE_CLIENT_SECRET_FILE points to {env_path}, which doesn't exist.")
        return Path(env_path)
    matches = list(BASE_DIR.glob(CLIENT_SECRET_GLOB))
    if not matches:
        raise GoogleAuthNotReadyError(
            f"No {CLIENT_SECRET_GLOB} file found in {BASE_DIR} — download an OAuth "
            "Client ID (Desktop app) JSON from the Google Cloud console first, or set "
            "CT_GOOGLE_CLIENT_SECRET_FILE to its path."
        )
    return matches[0]


def save_token(creds):
    """Writes the OAuth token encrypted with secret_store (it holds a refresh
    token with full Sheets access and the client secret)."""
    TOKEN_PATH.write_text(secret_store.encrypt(creds.to_json()))


def load_token_info():
    """The token as a dict. A token saved in plain text before 0.10.4 is read
    as-is and re-saved encrypted."""
    stored = TOKEN_PATH.read_text().strip()
    info = json.loads(secret_store.decrypt(stored))
    if not secret_store.is_encrypted(stored):
        TOKEN_PATH.write_text(secret_store.encrypt(json.dumps(info)))
    return info


def get_credentials():
    """Loads (and silently refreshes) the cached OAuth token. Raises
    GoogleAuthNotReadyError if the one-time consent flow hasn't been run."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    if not TOKEN_PATH.exists():
        raise GoogleAuthNotReadyError(
            f"{TOKEN_PATH} not found — run `python google_oauth_setup.py` once to authorize this app."
        )
    try:
        info = load_token_info()
    except secret_store.SecretUnreadable as e:
        raise GoogleAuthNotReadyError(f"{TOKEN_PATH.name} can't be decrypted ({e}) — run `python google_oauth_setup.py` again.") from e
    creds = Credentials.from_authorized_user_info(info, SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        save_token(creds)
    return creds


# Columns A:F. Owner email was added as column F in 0.10.0 so sheets created
# earlier keep Status in column E; the header cell F1 is added when missing.
#
# Column A ("ID") is the sheet's own sequence number since 0.10.2: 1, 2, 3...
# starting at 1 in an empty sheet (it used to be the app's internal task id,
# which kept counting after tasks were deleted). It is also how a row is found
# again: external_ref is "sheet-<n>" and the row is the one whose column A
# holds n — so deleting or sorting rows in the sheet can't point an update at
# the wrong task. Rows synced before 0.10.2 keep "row-<n>" (a row number).
HEADER_ROW = ["ID", "Task", "Owner", "Due date", "Status", "Owner email"]
STATUS_LABELS = {"pending": "Pending", "in_progress": "In progress", "done": "Done"}


class RowMissingError(LookupError):
    """The task's row is no longer in the sheet (deleted, or the sheet was cleared)."""


def _values_api():
    from googleapiclient.discovery import build

    return build("sheets", "v4", credentials=get_credentials()).spreadsheets().values()


def status_label(status):
    return STATUS_LABELS.get(status, status)


def row_values(c, number=""):
    """The A:F cells for one commitment; A is its sequence number in the sheet."""
    return [number, c["task_text"], c["owner_name"], c.get("due_date") or "", status_label(c["status"]),
            c.get("owner_email") or ""]


def _column_a(values_api, spreadsheet_id):
    return values_api.get(spreadsheetId=spreadsheet_id, range="A:A").execute().get("values", [])


def _cell(column_a, index):
    """Column A's value at a 0-based index, as stripped text ("" if empty)."""
    if index >= len(column_a) or not column_a[index]:
        return ""
    return str(column_a[index][0]).strip()


def _as_number(text):
    try:
        return int(float(text))
    except (TypeError, ValueError):
        return None


def find_row(column_a, external_ref):
    """The sheet row (1-based) holding this task, or None if it's gone."""
    kind, _, value = (external_ref or "").partition("-")
    if kind == "sheet":
        for index in range(1, len(column_a)):
            if _as_number(_cell(column_a, index)) == _as_number(value):
                return index + 1
        return None
    if kind == "row" and value.isdigit():
        n = int(value)
        return n if n >= 2 and _cell(column_a, n - 1) else None
    return None


def missing_refs(spreadsheet_id, external_refs):
    """The subset of external_refs whose rows are no longer in the sheet
    (one read of column A for all of them)."""
    column_a = _column_a(_values_api(), spreadsheet_id)
    return {ref for ref in external_refs if find_row(column_a, ref) is None}


def append_commitment_rows(spreadsheet_id, commitments):
    """Writes one row per commitment to columns A:F of the first tab below
    the last used row, adding the header row first if the sheet is empty (or
    just the "Owner email" header to a sheet made before 0.10.0). Column A
    gets the next sequence number: 1 in an empty sheet, else one more than
    the highest number already there. Returns one external_ref
    ("sheet-<number>") per commitment, in order.

    Deliberately not values.append: append guesses where the "table" is, and
    an empty cell (e.g. a commitment with no due date) splits that guess, so
    later rows land shifted to the right. Picking the next row from column A
    and writing an explicit A<n>:F<m> range keeps every row aligned."""
    if not commitments:
        return []
    values_api = _values_api()

    column_a = _column_a(values_api, spreadsheet_id)
    while column_a and not _cell(column_a, len(column_a) - 1):
        column_a.pop()        # trailing blank cells (cleared rows) don't count
    if not column_a:
        values_api.update(
            spreadsheetId=spreadsheet_id, range="A1:F1",
            valueInputOption="RAW", body={"values": [HEADER_ROW]},
        ).execute()
        column_a = [HEADER_ROW[:1]]
    elif column_a[0] == ["ID"]:
        values_api.update(
            spreadsheetId=spreadsheet_id, range="F1",
            valueInputOption="RAW", body={"values": [[HEADER_ROW[5]]]},
        ).execute()
    used = [n for n in (_as_number(_cell(column_a, i)) for i in range(1, len(column_a))) if n is not None]
    first_number = max(used) + 1 if used else 1
    first_row = len(column_a) + 1
    last_row = first_row + len(commitments) - 1

    values_api.update(
        spreadsheetId=spreadsheet_id, range=f"A{first_row}:F{last_row}",
        valueInputOption="RAW",
        body={"values": [row_values(c, first_number + i) for i, c in enumerate(commitments)]},
    ).execute()
    return [f"sheet-{first_number + i}" for i in range(len(commitments))]


def append_commitment_row(spreadsheet_id, commitment):
    return append_commitment_rows(spreadsheet_id, [commitment])[0]


def update_row(spreadsheet_id, external_ref, commitment):
    """Rewrites every editable cell (B:F — task, owner, due date, status,
    owner email) of an already-synced row, so the sheet matches the app after
    any change made here. Raises RowMissingError if the row is no longer in
    the sheet, rather than writing into whatever row is there now. An edit
    made through the API doesn't fire the sheet's On edit trigger, so this
    can't bounce back as an inbound update."""
    values_api = _values_api()
    n = find_row(_column_a(values_api, spreadsheet_id), external_ref)
    if n is None:
        raise RowMissingError(f"{external_ref} is no longer in the sheet")
    values_api.update(
        spreadsheetId=spreadsheet_id, range=f"B{n}:F{n}",
        valueInputOption="RAW", body={"values": [row_values(commitment)[1:]]},
    ).execute()
