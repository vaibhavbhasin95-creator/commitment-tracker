"""
Account administration from the machine the app runs on (0.9.0).

    python src/manage_users.py list
    python src/manage_users.py set-password you@example.com      # prompts twice, hidden
    python src/manage_users.py sign-out-everywhere you@example.com

set-password is how an account created before 0.9.0 (when signing in only
needed an email) gets its first password. Running it needs access to this
computer and the database file, which is what makes it safe: nobody can claim
an existing account over the web without either the password or SSO.
"""

import getpass
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import auth  # noqa: E402

DB_PATH = Path(__file__).resolve().parent.parent / "commitment_tracker.db"


def connect():
    if not DB_PATH.exists():
        sys.exit(f"No database at {DB_PATH} — start the app once first (python src/app.py).")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    auth.migrate(conn)
    conn.commit()
    return conn


def cmd_list(conn):
    rows = conn.execute("SELECT email, password_hash, last_login_at FROM users ORDER BY email").fetchall()
    links = {}
    for r in conn.execute("SELECT user_email, provider FROM user_identities"):
        links.setdefault(r["user_email"], []).append(r["provider"])
    for r in rows:
        methods = (["password"] if r["password_hash"] else []) + links.get(r["email"], [])
        print(f"{r['email']:40s} {', '.join(methods) or 'NO SIGN-IN METHOD (run set-password)':40s} last sign-in: {r['last_login_at'] or '—'}")


def cmd_set_password(conn, email):
    email = email.strip().lower()
    if conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone() is None:
        sys.exit(f"No account for {email}. Create one from the sign-in page instead.")
    password = getpass.getpass("New password: ")
    problem = auth.password_problem(password)
    if problem:
        sys.exit(f"Not changed — {problem}.")
    if getpass.getpass("Repeat it: ") != password:
        sys.exit("Not changed — the two entries didn't match.")
    conn.execute("UPDATE users SET password_hash = ?, session_version = session_version + 1 WHERE email = ?",
                 (auth.hash_password(password), email))
    conn.commit()
    print(f"Password set for {email}. Any existing sessions for it were signed out.")


def cmd_sign_out_everywhere(conn, email):
    cur = conn.execute("UPDATE users SET session_version = session_version + 1 WHERE email = ?", (email.strip().lower(),))
    conn.commit()
    print("Done." if cur.rowcount else "No such account.")


def main(argv):
    if len(argv) < 2 or argv[1] not in ("list", "set-password", "sign-out-everywhere"):
        sys.exit(__doc__)
    conn = connect()
    if argv[1] == "list":
        cmd_list(conn)
    elif len(argv) < 3:
        sys.exit(f"Usage: python src/manage_users.py {argv[1]} you@example.com")
    elif argv[1] == "set-password":
        cmd_set_password(conn, argv[2])
    else:
        cmd_sign_out_everywhere(conn, argv[2])


if __name__ == "__main__":
    main(sys.argv)
