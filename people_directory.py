"""
People directory: CSV parsing and matching a task owner's name to a person.

Transcripts only give names ("Bhuvan", "Kevin Heys"), but nudges need an email.
An account uploads a CSV of its people (First Name, Last Name, Email ID,
Corporate User Name), and every new task's owner name is looked up here.

Matching is deliberately conservative: a name is only auto-tagged when exactly
one person fits. If two people share the name it is marked "ambiguous" and left
for someone to pick the right email by hand, rather than guessing and nudging
the wrong person.
"""

import csv
import io
import re

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
NO_OWNER = {"", "unresolved", "unknown", "you"}

# Accepted header spellings, normalized (lowercase, letters only).
HEADER_ALIASES = {
    "first_name": {"firstname", "first", "givenname", "forename"},
    "last_name": {"lastname", "last", "surname", "familyname"},
    "email": {"emailid", "email", "emailaddress", "mail", "workemail"},
    "username": {"corporateusername", "username", "userid", "login", "corporateid", "samaccountname", "upn"},
}
TEMPLATE_CSV = "First Name,Last Name,Email ID,Corporate User Name\nKevin,Heys,kevin.heys@example.com,kheys\n"


def _norm(value):
    """Lowercase, strip accents-free punctuation, collapse spaces."""
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s@.\\-]", " ", str(value or "").lower())).strip()


def _username_forms(username):
    """'DOMAIN\\jsmith' and 'jsmith@corp' also match plain 'jsmith'."""
    u = _norm(username)
    if not u:
        return set()
    forms = {u, u.split("\\")[-1], u.split("@")[0]}
    return {f for f in forms if f}


def parse_csv(text):
    """Returns (people, errors). people: list of dicts with first_name,
    last_name, email, username. errors: list of "row N: problem" strings.
    Raises ValueError if the header row has no email column."""
    text = text.lstrip("﻿")
    reader = csv.reader(io.StringIO(text))
    try:
        header = next(reader)
    except StopIteration:
        raise ValueError("the file is empty")

    columns = {}
    for index, name in enumerate(header):
        key = re.sub(r"[^a-z]", "", name.lower())
        for field, aliases in HEADER_ALIASES.items():
            if key in aliases and field not in columns:
                columns[field] = index
    if "email" not in columns:
        raise ValueError("no email column found — expected a header like 'Email ID'")

    people, errors, seen = [], [], set()
    for row_number, row in enumerate(reader, start=2):
        if not any(cell.strip() for cell in row):
            continue
        get = lambda f: row[columns[f]].strip() if f in columns and columns[f] < len(row) else ""
        email = get("email").lower()
        if not EMAIL_RE.match(email):
            errors.append(f"row {row_number}: '{get('email')}' isn't a valid email — skipped")
            continue
        if email in seen:
            errors.append(f"row {row_number}: {email} appears more than once — kept the first")
            continue
        seen.add(email)
        people.append({
            "first_name": get("first_name"),
            "last_name": get("last_name"),
            "email": email,
            "username": get("username"),
        })
    return people, errors


def match_owner(name, people):
    """Finds the person a spoken owner name refers to.

    Returns (email_or_None, status, candidate_emails):
      status 'auto'      — exactly one person matches; email is theirs
      status 'ambiguous' — several people match; candidates lists them
      status 'none'      — nobody matches (or the name is UNRESOLVED/UNKNOWN)

    Tiers are tried in order and the first tier with any match decides:
      1. exact email / corporate user name / email local part ("kevin.heys")
      2. full name ("kevin heys"), also ignoring middle names
      3. a single spoken word that is someone's first name, or else last name
    """
    spoken = _norm(name)
    if spoken in NO_OWNER or not people:
        return None, "none", []
    words = spoken.split(" ")

    def full(p):
        return _norm(f"{p['first_name']} {p['last_name']}")

    tiers = [
        lambda p: spoken == _norm(p["email"]) or spoken in _username_forms(p["username"])
                  or spoken == _norm(p["email"]).split("@")[0],
        lambda p: bool(p["first_name"] and p["last_name"]) and (
            spoken == full(p)
            or (len(words) >= 2 and words[0] == _norm(p["first_name"]).split(" ")[0]
                and words[-1] == _norm(p["last_name"]).split(" ")[-1])),
    ]
    if len(words) == 1:
        tiers.append(lambda p: words[0] == _norm(p["first_name"]).split(" ")[0] if p["first_name"] else False)
        tiers.append(lambda p: words[0] == _norm(p["last_name"]).split(" ")[-1] if p["last_name"] else False)

    for tier in tiers:
        found = sorted({p["email"] for p in people if tier(p)})
        if len(found) == 1:
            return found[0], "auto", found
        if len(found) > 1:
            return None, "ambiguous", found
    return None, "none", []
