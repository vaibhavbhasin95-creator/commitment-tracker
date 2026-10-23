"""
One-time local OAuth consent flow for the Google Sheets integration.

Run this once before using Google Sheets as a task tracker. It opens a
browser tab, you log in with the Google account that owns the target
spreadsheet and approve access, and it saves a refresh token to
google_token.json — after that, app.py's real Sheets API calls run without
any further interaction. Re-run it if google_token.json is ever deleted or
the granted scope changes.

Needs a client_secret_*.json file (an OAuth Client ID, Desktop app type)
from the Google Cloud console in the project root (one level above src/), with the Google Sheets
API enabled for that project.

Run (from the project root):
    python src/google_oauth_setup.py
"""

from google_sheets import TOKEN_PATH, SCOPES, find_client_secret_path, save_token


def main():
    from google_auth_oauthlib.flow import InstalledAppFlow

    client_secret_path = find_client_secret_path()
    print(f"Using client secret: {client_secret_path.name}")
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secret_path), SCOPES)
    creds = flow.run_local_server(port=0)
    save_token(creds)
    print(f"Saved credentials (encrypted) to {TOKEN_PATH} — Google Sheets sync is ready to use.")


if __name__ == "__main__":
    main()
