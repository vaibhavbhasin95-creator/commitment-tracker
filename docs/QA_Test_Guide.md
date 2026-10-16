# Commitment Tracker — QA Test Guide

**For:** QA team · **App version under test:** {{VERSION}} · **Prepared:** {{DATE}}

## 1. What you are testing

Commitment Tracker turns what people promise in meetings ("I'll send the budget by Friday") into tracked tasks:

1. A **meeting transcript** comes in (pasted, uploaded as `.txt` / `.docx` / `.pdf`, or pushed by a recording tool).
2. The app **extracts the commitments**: task, owner and due date.
3. Each task is **matched to a person** from the People directory, which gives the task an owner email.
4. Tasks are tracked on the **dashboard**, and can be kept in sync **both ways with a Google Sheet**.
5. Owners get **reminder emails (nudges)** before or after the due date.

It is a web app. You only need a browser (Chrome or Edge); nothing has to be installed on your machine.

### In scope

| Area | What to cover |
|---|---|
| Sign-in | Sign-up, sign-in, wrong password, lockout, sign-out, password change |
| Transcripts | Paste, upload `.txt` / `.docx` / `.pdf`, rejected files, demo meeting |
| Extraction | Background extraction, task / owner / due date found, editing results |
| Dashboard | Per-meeting view, status changes, filters, clear actions, live updates |
| People directory | CSV upload, owner matching, ambiguous names, manual owner pick |
| Google Sheets | App → Sheet and Sheet → App sync, row numbering, deleted rows, "Sync all" |
| Email nudges | Test email, manual nudge, automatic nudges by cadence, AI-tailored drafts (optional) |
| Webhook security | Signing secret reveal / rotate, delivery log |
| AI extraction (optional) | Own AI key, test connection, fallback to the built-in model |

### Out of scope (do not log as bugs)

These parts are **simulated or not built yet**:

- **Jira** and **Custom tracker** sync — only a mock "would create issue" log.
- **Zoom / Google Meet / Teams / Otter** "Check now" — mocked. (Fathom is the real recording source; it is tested separately by the developer.)
- **Slack, Teams, custom-webhook nudges** — built but not part of this round unless you are asked to.
- **Single sign-on (SSO)** — not configured on the test server.
- **Password reset by email, email verification, MFA, OCR for scanned PDFs, mobile layout** — not built.
- **Shared team workspaces** — every account sees only its own meetings and tasks. Two testers can't see each other's data; this is by design.

> **Extraction quality is a known limitation.** The built-in model was trained on synthetic meetings, so on real conversations it can miss commitments or flag extra sentences. Record what you see under "Extraction observations" (section 7) rather than as a defect, unless the app crashes, hangs or loses data.

## 2. How you get access — no setup needed

**You do not need to install anything or repeat the developer's setup.** The developer runs the app on their machine and shares it over a secure public link (ngrok):

| Item | Value | Used for |
|---|---|---|
| **Test URL** | `https://drowsily-posture-unmoving.ngrok-free.dev` | Opening the app |
| **Host Google account** | `vaibhavbhasin95@gmail.com` | Sharing your test Google Sheet with the app (section 4.3) |
| **Test window** | Sent by the developer with this guide | The app is only reachable while the host's machine is running |

Rules for the shared server:

- **Create your own account** (section 3). Do not use the developer's account: it is connected to their real Google Sheet, AI key and mailbox.
- **Use test data only.** Do not paste confidential or real meeting transcripts.
- The first time you open the link, ngrok shows a **"You are about to visit…"** page. Click **Visit Site**. This is expected.
- If the URL stops responding, the host's machine or tunnel is down. Tell the developer; this is not an app defect.
- The **first extraction after the server starts can take several minutes** while the ML model loads. Later ones take seconds.

## 3. First steps (every tester, ~5 minutes)

1. Open the **Test URL** and click **Visit Site** on the ngrok page.
2. On the sign-in page choose **Create account**. Use your work email and a password of **at least 10 characters**.
3. You land on the **Dashboard**. Check that the sidebar shows **version {{VERSION}}** at the bottom.
4. Click **Load demo meeting**. A demo meeting with tasks appears. This confirms your account works.

The sidebar pages are:

| Page | Purpose |
|---|---|
| **Dashboard** | Meetings, tasks, upload, status, sync buttons |
| **Background jobs** | Extraction / sync / nudge jobs with retry and cancel |
| **People** | People directory (CSV upload) |
| **AI extraction** | Optional: use your own AI engine instead of the built-in model |
| **Integrations** | Email nudges, Google Sheet tracker, transcript source, webhook security |
| **Account** | Change password, sign out everywhere |

## 4. Optional setup per area

Do only the parts for the areas you are assigned.

### 4.1 People directory (needed for owner matching and nudges)

1. On **People**, click **Download a template**. Columns: `First Name, Last Name, Email ID, Corporate User Name, Designation`.
2. Fill in 2–3 people whose names appear in the sample transcript (section 5). To receive all reminder emails yourself, use **plus-addresses** of your own Gmail, e.g. `you+priya@gmail.com`, `you+arjun@gmail.com`.

```
First Name,Last Name,Email ID,Corporate User Name,Designation
Priya,Shah,you+priya@gmail.com,pshah,VP Finance
Arjun,Mehta,you+arjun@gmail.com,amehta,Intern
```

3. Upload the CSV.

### 4.2 Email nudges (needs a Gmail account with 2-Step Verification)

1. Create a Gmail **app password** at `myaccount.google.com/apppasswords` (name it "Commitment Tracker"). Copy the 16-character code.
2. In the app: **Integrations → Nudges**, channel **Email**. SMTP server `smtp.gmail.com`, port `587`, security STARTTLS.
3. User name and Send from: your Gmail address. Password / app password: the **app password**, not your normal password.
4. Cadence: **1 day before due**. **Save channel**, then click **Send test** — a sample reminder should arrive.

### 4.3 Google Sheet (two-way sync)

1. Create a **blank Google Sheet** in your own Google account.
2. **Share** it with the **Host Google account** as **Editor**. The app writes to sheets through the host's Google login, so it needs this access.
3. Copy the sheet ID from its URL (the part between `/d/` and `/edit`).
4. In the app: **Integrations → Task tracker**, choose **Google Sheets**, paste the ID, tick **Auto-sync**, **Save**.
5. For Sheet → App: on the same page, copy the **Apps Script** shown. In the sheet: **Extensions → Apps Script**, replace everything with the copied script, save.
6. In Apps Script, click the clock icon (**Triggers**) → **Add Trigger**: function `onStatusEdit`, event source **From spreadsheet**, event type **On edit**. Approve the permission prompt (Google calls it "unverified" because it's your own script; that's expected).

The script already contains your personal webhook URL and signing secret. Don't share it.

### 4.4 AI extraction (optional)

Only if you are testing this area. Get a free **Google Gemini** key at `aistudio.google.com` → **Get API key**. In the app: **AI extraction**, engine **OpenAI-compatible**, provider **Google Gemini**, paste the key, model e.g. `gemini-2.5-flash`, tick consent, **Save**, **Test connection**. The free tier lets Google use prompts: **test data only**.

## 5. Sample transcript

On the Dashboard click **Upload transcript** and paste this, or save it as a `.txt` file to test the file upload:

```
Priya Shah: Thanks for joining the Q4 launch sync.
Priya Shah: I'll send the revised budget to finance by Friday.
Arjun Mehta: I will book the vendor demo by 30 October.
Priya Shah: Arjun, can you also update the launch checklist?
Arjun Mehta: Sure, I'll update the launch checklist by tomorrow.
Priya Shah: Great, that's everything for today.
```

**Expected result from the built-in model** (checked by the developer against this exact text):

| # | Task | Owner | Due date |
|---|---|---|---|
| 1 | send the revised budget to finance by Friday | Priya Shah | The next Friday after the meeting date (a meeting held on a Friday gets the following Friday) |
| 2 | book the vendor demo by 30 October | Arjun Mehta | **Blank.** Known limitation: dates written as "30 October" aren't read yet. Set it by hand (test C2) |
| 3 | update the launch checklist by tomorrow | Arjun Mehta | The day after the meeting date |

The other three lines must **not** become tasks. With the People directory loaded (4.1), tasks 1–3 also get the owner's email.

## 6. Test cases

Status values in the app: **Pending**, **In progress**, **Done**. "Expected" is the pass condition.

### A. Sign-in and account

| ID | Steps | Expected |
|---|---|---|
| A1 | Create an account with a 9-character password | Refused: "use at least 10 characters" |
| A2 | Create an account with a valid password | Signed in, Dashboard shown |
| A3 | Sign out, sign in with a wrong password | Generic error; does not reveal whether the email exists |
| A4 | Enter a wrong password 5 times | Account locked for 15 minutes, even with the right password |
| A5 | Signed out, open `https://drowsily-posture-unmoving.ngrok-free.dev/dashboard` directly | Redirected to sign-in |
| A6 | Account → change password; sign in again with the new password | Works; other open sessions are signed out |
| A7 | Sign in on two browsers; Account → **Sign out everywhere** | Both sessions end |
| A8 | Two testers create accounts; each loads data | Neither sees the other's meetings or tasks |

### B. Transcripts and extraction

| ID | Steps | Expected |
|---|---|---|
| B1 | Paste the sample transcript (section 5) | Meeting appears at once with "Extracting…"; tasks appear when done, without refreshing |
| B2 | Upload the sample as `.txt` | Same as B1 |
| B3 | Upload a Word `.docx` with a short meeting | Text read, tasks extracted |
| B4 | Upload a `.pdf` that has selectable text | Text read, tasks extracted |
| B5 | Upload a scanned PDF (image only), an old `.doc`, or an image renamed to `.pdf` | Refused with a clear reason; no crash |
| B6 | Upload a file over 20 MB | Refused with a size message |
| B7 | **Background jobs** page during / after B1 | Extraction job listed with its status and the engine used |
| B8 | Open a meeting → re-run extraction | Job queued; tasks refreshed |
| B9 | **Load demo meeting** | Demo meeting and tasks appear |

### C. Dashboard and tasks

| ID | Steps | Expected |
|---|---|---|
| C1 | Select one meeting, then "All meetings" | Task list switches between that meeting and everything |
| C2 | Open a task; edit task text, owner, due date; save | Saved; shown on the dashboard |
| C3 | Change status Pending → In progress → Done | Status and the status chart update |
| C4 | Add a task by hand | Appears under the chosen meeting |
| C5 | **Clear this meeting's tasks** | Confirmation asked; only that meeting's tasks go |
| C6 | **Clear all meetings** | Confirmation asked; everything removed; Google Sheet rows untouched |
| C7 | Dashboard open in two tabs; change a status in one | Other tab updates within a few seconds, without refreshing |

### D. People directory and owners

| ID | Steps | Expected |
|---|---|---|
| D1 | Upload the People CSV (4.1), then paste the sample transcript | Tasks owned by Priya / Arjun get their email automatically |
| D2 | Upload a CSV with two people with the same first name; extract a task naming only that first name | Owner flagged as **ambiguous**; not guessed |
| D3 | Pick an owner by hand on a task | Saved; a later CSV upload does not overwrite it |
| D4 | Upload a CSV with a missing Email ID column / bad email | Clear error for the bad rows |
| D5 | Edit a person's designation on People | Saved |

### E. Google Sheets sync (needs 4.3)

| ID | Steps | Expected |
|---|---|---|
| E1 | Auto-sync on; add a meeting with tasks | Rows appear: A **ID**, B Task, C Owner, D Due date, E Status, F Owner email. In an empty sheet IDs start at **1** |
| E2 | Change a task's status / due date / owner in the app | The same row updates in the sheet |
| E3 | In the sheet, change a row's **Status** to `Done` | Dashboard updates within seconds, with a pop-up |
| E4 | In the sheet, change a row's Task, Owner, Due date or Owner email | The app updates that task |
| E5 | Sort the sheet, or insert rows above existing ones; then edit a task in the app | The correct row (matched by column A ID) updates, not whichever row is now in that position |
| E6 | Delete a task's row in the sheet; edit that task in the app | Nothing is written to another row; the sync history says the task was unlinked |
| E7 | Delete all rows (header too), press **Sync all to sheet** | Sheet rebuilt, numbered from 1; the message reports how many were re-added |
| E8 | Edit the same task in the app and the sheet one after the other | The most recent edit wins |
| E9 | Turn Auto-sync off; add a task; press **Sync to tracker** on it | Only then does it appear in the sheet |

### F. Email nudges (needs 4.1 and 4.2)

| ID | Steps | Expected |
|---|---|---|
| F1 | Integrations → **Send test** on the email channel | Sample email arrives |
| F2 | On a task with an owner email, **Send test nudge** | Email arrives at the owner's address |
| F3 | Task due tomorrow; switch on **Automatic nudges** | One reminder arrives (the server checks every 15 min, or sooner if the host shortened the interval); no repeat on the next check |
| F4 | Mark the task Done before the next check | No further reminders for it |
| F5 | Enter a wrong app password, then **Send test** | Clear SMTP error in **Recent nudges**; no crash |
| F6 | Edit the email draft on Integrations; **Preview** | Placeholders (`{owner_first_name}`, `{task}`, `{due_date}`) filled in |
| F7 | (With 4.4) turn on AI tailoring; preview for the VP and for the Intern | Wording differs by designation; facts and dates unchanged |

### G. Webhook security and AI extraction

| ID | Steps | Expected |
|---|---|---|
| G1 | Integrations → Webhook security → **Reveal** / **Copy** the secret | Secret shown only on request |
| G2 | **Rotate** the secret; edit a status in the sheet without updating the Apps Script | Sheet → App stops (rejected, listed in recent deliveries); re-paste the new script and it works again |
| G3 | Integrations → **Simulate incoming transcript** | Meeting appears on the dashboard live, with tasks |
| G4 | (4.4) AI extraction → **Test connection** | Commitments listed from a sample |
| G5 | (4.4) Enter an invalid model name; upload a transcript | Falls back to the built-in model; the run history says why |

## 7. Reporting

Log each defect with:

| Field | Example |
|---|---|
| Test ID | E5 |
| Title | Sheet edit updates the wrong row after sorting |
| Steps | Exact clicks, and the data used |
| Expected / actual | From the table vs what happened |
| Evidence | Screenshot; for errors also the browser console (F12 → Console) |
| Account email and time | So the developer can find it in the server log |
| Version | Shown at the bottom of the sidebar ({{VERSION}}) |
| Severity | Blocker / Major / Minor / Cosmetic |

**Extraction observations** (not defects): for each transcript, list commitments the app **missed** and sentences it **wrongly** flagged. This feeds the model retraining.

## Appendix A — For the host (developer) only

Run before each test window, from `C:\PersonalDevelopment\commitment-tracker` with the project's Python:

```
$env:CT_COOKIE_SECURE = "1"
$env:CT_PUBLIC_URL = "https://drowsily-posture-unmoving.ngrok-free.dev"
$env:NUDGE_CHECK_INTERVAL_SECONDS = "60"     # optional: faster automatic-nudge checks for F3
python src\app.py
```

In a second terminal:

```
ngrok http --url=drowsily-posture-unmoving.ngrok-free.dev 5000
```

- Keep both running for the whole window. Don't use `CT_DEBUG=1` on a public link.
- Send the testers the test window. The Test URL and the Google account that owns `google_token.json` are already in section 2.
- If Sheet sync fails with `invalid_grant`, the Google token has expired (7 days in "Testing" mode): run `python src\google_oauth_setup.py` again.
- Your own account's Google Sheet auto-sync and nudge channels are untouched: settings are per account.
- Optionally, after testing, set `CT_ALLOW_SIGNUP=0` to stop new accounts.

## Appendix B — Running a private copy (only if a tester needs one)

Not needed for this round. If someone must test offline:

1. `git clone https://github.com/vaibhavbhasin95-creator/commitment-tracker.git` and check out the branch named by the developer.
2. Python 3.11+: `pip install -r requirements.txt` and `python -m spacy download en_core_web_sm` (several GB, mainly TensorFlow).
3. `python src/app.py`, open `http://localhost:5000`.
4. Google Sheets needs their own Google Cloud OAuth client (`client_secret_*.json`) and `python src/google_oauth_setup.py`. Never copy the developer's `secret.key`, `flask_secret.key`, `google_token.json`, `client_secret_*.json` or `commitment_tracker.db`. They are the developer's credentials and data.
