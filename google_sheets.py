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

from pathlib import Path

BASE_DIR = Path(__file__).parent
TOKEN_PATH = BASE_DIR / "google_token.json"
CLIENT_SECRET_GLOB = "client_secret_*.json"
SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]


class GoogleAuthNotReadyError(RuntimeError):
    """Raised when google_token.json doesn't exist yet, or no client-secret
    file is present — run `python google_oauth_setup.py` first."""


def find_client_secret_path():
    matches = list(BASE_DIR.glob(CLIENT_SECRET_GLOB))
    if not matches:
        raise GoogleAuthNotReadyError(
            f"No {CLIENT_SECRET_GLOB} file found in {BASE_DIR} — download an OAuth "
            "Client ID (Desktop app) JSON from the Google Cloud console first."
        )
    return matches[0]


def get_credentials():
    """Loads (and silently refreshes) the cached OAuth token. Raises
    GoogleAuthNotReadyError if the one-time consent flow hasn't been run."""
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    if not TOKEN_PATH.exists():
        raise GoogleAuthNotReadyError(
            f"{TOKEN_PATH} not found — run `python google_oauth_setup.py` once to authorize this app."
        )
    creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
        TOKEN_PATH.write_text(creds.to_json())
    return creds


HEADER_ROW = ["ID", "Task", "Owner", "Due date", "Status"]
STATUS_LABELS = {"pending": "Pending", "in_progress": "In progress", "done": "Done"}


def _values_api():
    from googleapiclient.discovery import build

    return build("sheets", "v4", credentials=get_credentials()).spreadsheets().values()


def status_label(status):
    return STATUS_LABELS.get(status, status)


def append_commitment_rows(spreadsheet_id, commitments):
    """Writes one row per commitment (id, task, owner, due date, status) to
    columns A:E of the first tab in a single API call, adding a header row
    first if the sheet is empty. Returns one external_ref ("row-<n>") per
    commitment, in order — the reverse (Sheet -> App) flow keys off the row
    number of an edited status cell in column E.

    Deliberately not values.append: append guesses where the "table" is, and
    an empty cell (e.g. a commitment with no due date) splits that guess, so
    later rows land shifted to the right. Picking the next row from column A
    and writing an explicit A<n>:E<m> range keeps every row aligned."""
    if not commitments:
        return []
    values_api = _values_api()

    column_a = values_api.get(spreadsheetId=spreadsheet_id, range="A:A").execute().get("values", [])
    if not column_a:
        values_api.update(
            spreadsheetId=spreadsheet_id, range="A1:E1",
            valueInputOption="RAW", body={"values": [HEADER_ROW]},
        ).execute()
        column_a = [HEADER_ROW[:1]]
    first_row = len(column_a) + 1
    last_row = first_row + len(commitments) - 1

    rows = [[
        c["id"],
        c["task_text"],
        c["owner_name"],
        c.get("due_date") or "",
        status_label(c["status"]),
    ] for c in commitments]
    values_api.update(
        spreadsheetId=spreadsheet_id, range=f"A{first_row}:E{last_row}",
        valueInputOption="RAW", body={"values": rows},
    ).execute()
    return [f"row-{first_row + i}" for i in range(len(commitments))]


def append_commitment_row(spreadsheet_id, commitment):
    return append_commitment_rows(spreadsheet_id, [commitment])[0]


def update_status(spreadsheet_id, external_ref, status):
    """Overwrites the status cell (column E) of an already-synced row. An
    edit made through the API doesn't fire the sheet's On edit trigger, so
    this can't bounce back into the app as an inbound update."""
    row_number = int(external_ref.split("-", 1)[1])
    _values_api().update(
        spreadsheetId=spreadsheet_id, range=f"E{row_number}",
        valueInputOption="RAW", body={"values": [[status_label(status)]]},
    ).execute()
