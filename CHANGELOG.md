# Changelog

All notable changes to Commitment Tracker are recorded here, newest first.

## How versioning works

- Version numbers follow **MAJOR.MINOR.PATCH** ([Semantic Versioning](https://semver.org)):
  - **MINOR** (0.**x**.0) — a new feature or capability.
  - **PATCH** (0.x.**y**) — bug fixes and small corrections to existing behavior.
  - **MAJOR** stays at 0 while the app is a prototype; 1.0.0 marks the first release meant for real customers.
- The current version lives in one place: `APP_VERSION` in [src/app.py](src/app.py). It is shown in the sidebar of every page and returned by `GET /api/version`.
- Every change gets an entry here in the same session it is made, and `APP_VERSION` is bumped to match.
- Sections per version: **Added** (new features), **Changed** (changes to existing behavior), **Fixed** (bug fixes), **Security**, **Known issues**.
- **Revertible features:** a feature built "on trial" gets a snapshot of every file it modifies under `_snapshots/<previous version>/`, plus a revert script and instructions in its changelog entry (first used for 0.5.0).
- **Git status:** only 0.1.0 is committed to git (commits `b92fcc9`–`1ad95f9`). Everything from 0.2.0 onward exists as uncommitted local changes — no commits or tags have been made for them yet.

---

## [0.10.0] — 2026-10-15 — Every field syncs both ways with Google Sheets; the most recent edit wins

### Added
- **Owner email in the sheet.**
  - The sheet now has six columns: A ID, B Task, C Owner, D Due date, E Status, **F Owner email**.
  - The email was added as column F so a sheet made earlier keeps Status in column E. Its "Owner email" header is added on the next write.
- **App → sheet, every field.** Changing a task's text, owner, owner email, due date or status in the app rewrites the whole row (B:F) in the sheet, both on auto-sync and on "Sync to tracker". Before, only status was pushed after a row's first write.
- **Sheet → app, every field.**
  - The tracker webhook accepts any of `task`, `owner`, `owner_email`, `due_date`, `status`; fields left out are unchanged.
  - **The Apps Script on the Integrations page now sends the whole row for any edit in columns B–F**, including pasted ranges, and formats date cells as YYYY-MM-DD. Its function name `onStatusEdit` is unchanged, so the existing trigger keeps working once the script is replaced.
  - An owner renamed in the sheet without an email is matched from People, and the email is written into column F.
  - An email typed in the sheet counts as a manual pick, which re-matching never overwrites.
  - A bad date or email from the sheet is refused with a reason.
- **The most recent edit wins.**
  - Tasks have a new `updated_at` column, set by every change in the app or from the sheet. Existing databases get it on start.
  - An edit from the sheet carries its time (`edited_at`, or the signed request's timestamp). If it's older than the app's last change, it's ignored, and the app's version is written back to the row so both sides show the winner. A tie goes to the sheet.
  - Accepted and ignored edits both appear in the task's sync history.
- **Tidying sheet values.** After an accepted sheet edit, cells the app normalises are written back: a status typed "Closed" becomes "Done", and a matched email is filled in. Writes through the API don't fire the sheet's On edit trigger, so this can't loop.
- **Task editing in the app.** The task modal on the dashboard has **Edit details** (task, owner name, due date). `PATCH /api/commitments/<id>` accepts `task_text`, `owner_name` and `due_date` as well as `status` and `owner_email`, and returns the list of fields that `changed`.
- **New suite `src/tests/test_v010_sheet_sync.py`: 21 checks.** Total: 279 checks, all passing.

### Changed
- `google_sheets.update_row()` writes B:F; `update_status()` is kept but no longer used by the app.
- `test_sheets_sync.py` expects the six-column layout and the "Closed" → "Done" tidy-up.

### Action needed
- **Replace the Apps Script in the sheet** with the new one from Integrations → Task tracker. The 0.9.x script only sends status.

### Git
- **First commit since 0.7.3 (0.8.0–0.10.0), on a new branch `release/v0.10.0`.** It was not pushed.
- **No credentials or confidential data are in it:**
  - Credentials and keys stay git-ignored: the database, `secret.key`, `flask_secret.key`, `auth_config.json`, Google client secret and token, and `_snapshots/`.
  - **The FY2027 transcripts (`FY2027_Financial_Planning_Meeting_Transcript.docx` / `.txt`), real meeting content, were removed from tracking and added to `.gitignore`.** They stay on disk and in earlier commits already pushed to GitHub.

---

## [0.9.1] — 2026-10-15 — Clear message when the app runs on the wrong Python

### Fixed
- **Problem:** started with the Windows Store Python instead of the project's `.venv`, a Google Sheets sync failed with "No module named 'googleapiclient'" and was retried 5 times. That Python also lacks TensorFlow, pypdf and anthropic.
- **Startup warning:** `python src/app.py` now prints a warning listing any missing packages and what each one breaks (Sheets sync, `.pdf` upload, built-in extraction, Claude). It gives the command to use instead: `..\.venv\Scripts\python src\app.py`.
- **Sheets sync jobs:** a job that fails because a library isn't installed now fails at once, with that instruction, instead of retrying.

---

## [0.9.0] — 2026-10-15 — Real sign-in, signed webhooks, background jobs, .docx/.pdf upload, AI-tailored email nudges, code in src/

### Answered: does AI extraction work for uploaded transcripts?
Yes, and it always did since 0.5.0. Every capture path goes through the same extraction step: pasted text, uploaded files (now including `.docx` and `.pdf`), the capture and Fathom webhooks, and the "Extract commitments" button. When an account has saved and confirmed an AI engine on the AI extraction page, that engine reads the transcript; the built-in ML model is used only when no AI engine is chosen, or as a fallback when the AI call fails. What was missing was visibility. Now the upload result, the toast and the Background jobs page name the engine that ran (e.g. "Claude (Anthropic API) · claude-opus-5"), including when it fell back ("Built-in model (… failed — see the AI extraction page)").

### Changed — all code moved into `src/`
- Moved into `src/`:
  - the app modules and `schema.sql`;
  - `static/` and `tests/`;
  - the ML training scripts (`src/ml/`, including `labels.py` and the Colab export);
  - the doc builders (`src/docs_tools/`).
- **Runtime data stays in the project root, unmoved:** `commitment_tracker.db`, `secret.key`, the Google credentials, and the models in `ml/`. Every path in the code was updated to find them there. This includes `secret_store.KEY_PATH`; leaving it in `src/` would have generated a new key and made every saved credential unreadable.
- **Commands are now run from the project root:**
  - `python src/app.py`
  - `python src/tests/run_all.py`
  - `python src/google_oauth_setup.py`
  - `python src/ml/train_action_detector.py`
  - `python src/docs_tools/build_prd_pdf.py`
- Files were moved with plain `mv`, not `git mv`; the git index is untouched.

### Added — real sign-in (`auth.py`, `manage_users.py`)
- **Email + password accounts.**
  - Passwords are stored as salted scrypt hashes, at least 10 characters.
  - Five failures per email or IP lock sign-in for 15 minutes.
  - A wrong email and a wrong password get the same message, and the timing is equalised.
- **Single sign-on with any OpenID Connect provider** (Google, Microsoft Entra ID, Okta, Auth0, Keycloak), configured in `auth_config.json` (example: `auth_config.example.json`) or `CT_SSO_*` environment variables.
  - Uses the authorization-code flow with PKCE, state and nonce.
  - Checks the token's issuer (including Microsoft's multi-tenant issuer), audience, expiry and verified email.
  - Can limit sign-in to allowed email domains.
  - A verified SSO email links to an existing account with that email.
- **Sessions.**
  - The signing key now comes from the generated `flask_secret.key` or `COMMITMENT_TRACKER_FLASK_SECRET`; it is no longer hard-coded in source.
  - Cookies are HttpOnly and SameSite=Lax; set `CT_COOKIE_SECURE=1` behind HTTPS.
  - Sessions last 14 days.
  - A password change, or "Sign out everywhere", ends every other session.
- **CSRF guard:** every state-changing `/api/` call needs the `X-CT-Request: 1` header (sent by `app.js`). Webhooks are exempt; they're signed instead.
- App pages redirect to sign-in when signed out.
- New **Account page** (click your name in the sidebar): change or set a password, sign out everywhere.
- `python src/manage_users.py set-password <email>` gives a pre-0.9.0 account its first password. Only someone at this computer can do it, so nobody can claim an existing account over the web.
- `POST /api/identify` (email-only sign-in) now answers **410**.
- The Werkzeug debugger is off unless `CT_DEBUG=1` (it was always on).
- Responses carry `X-Content-Type-Options`, `X-Frame-Options: DENY` and `Referrer-Policy`.

### Added — webhook security (`webhook_security.py`)
- **Each account has a signing secret.**
  - It is encrypted at rest.
  - Integrations → Webhook security can reveal, copy or rotate it.
  - Senders sign `"<timestamp>.<body>"` with HMAC-SHA256: headers `X-CT-Timestamp`, `X-CT-Signature: v1=<hex>`, and optionally `X-CT-Delivery`.
- **Refused requests:**
  - unsigned or wrongly signed requests;
  - a changed body;
  - timestamps more than 5 minutes off;
  - replays of an accepted delivery;
  - more than 60 requests a minute per URL.
- **Fathom** keeps its own signature check, and now also gets the timestamp window and one-time `webhook-id`s. This fixes the 0.7.3 known issue that a captured Fathom request could be replayed.
- **Tracker status updates moved to `POST /api/webhooks/tracker-update/<token>` (signed).** The old unsigned `/api/webhooks/tracker-update`, which trusted an email plus a row reference, now answers **410**. `CT_ALLOW_UNSIGNED_TRACKER_WEBHOOK=1` re-enables it as a temporary bridge.
- **Action needed:** replace the Google Apps Script with the signed version on the Integrations page.
- The generic transcript-capture webhook must now be signed too.
- Every delivery, accepted or refused, is logged and shown on the Integrations page; entries are kept for 7 days.

### Added — background jobs (`jobs.py`, Background jobs page)
- A persistent job queue in SQLite, worked by one thread inside the app. These now run as jobs instead of inside the request:
  - extraction after an upload, a webhook, a simulated capture, or "Extract commitments";
  - retries of a failed Google Sheets auto-sync, as its own job with up to 5 attempts;
  - the automatic nudge check, which replaces the old scheduler thread and queues one run per account, never piling up.
- **Retries:** temporary failures retry with exponential backoff (30 s, 1 min, 2 min…). A permanent failure (e.g. "the model isn't trained") fails at once with the reason.
- **Restarts:** jobs left running by a stopped app are re-queued on start. Finished jobs are kept for 14 days.
- **Faster replies:** uploads and webhooks return immediately, so Fathom gets a fast reply. The dashboard shows "Extracting…" on the meeting and updates live when the job finishes (new `job_updated` event).
- **New "Background jobs" page:** counts, results, errors and next retry time, plus Retry and Cancel.
- `CT_JOBS_INLINE=1` runs jobs synchronously; the tests use this.

### Added — .docx / .pdf upload (`document_text.py`)
- **Supported files:** `.txt`, `.docx`, `.pdf`, `.vtt`, `.srt` and `.md`, up to 20 MB, converted to text on the server.
  - `.docx` uses the standard library, and a two-column speaker | words table becomes "Speaker: words".
  - `.pdf` uses `pypdf`, which was already a requirement.
  - Text files in Windows-1252 or UTF-16 are decoded too.
- The converted text goes through exactly the same extraction as pasted text, so it gets the built-in model or the AI engine.
- **Refused with a clear reason:**
  - scanned PDFs (no text layer; there's no OCR);
  - password-protected PDFs;
  - old `.doc` files;
  - renamed files;
  - oversized files, including zip bombs inside a `.docx`.
- Checked against the real FY2027 `.docx`, whose text matches the `.txt` version, and a text PDF.

### Added — per-meeting view and clearing (dashboard)
- **Per-meeting view:** the dashboard shows **one selected meeting's tasks**; the status chart and owner filter follow the selection.
  - Click a meeting to select it, or pick "All meetings". Its "Details" button opens the meeting.
  - The selection is remembered per browser and defaults to the newest meeting.
  - A new upload is selected automatically.
- **Meeting chips** show a task count and the extraction state ("Extracting…" or "Extraction failed").
- **Clearing data**, each with a confirmation:
  - "Clear this meeting's tasks" or "Clear all tasks" keeps the meetings and transcripts, so they can be re-extracted;
  - "Delete meeting" is in the meeting details;
  - "Clear all meetings" removes every meeting.
- **What a delete removes:** the task's nudge and sync history goes with it, and queued extraction jobs are cancelled. Rows already in the Google Sheet are **not** touched.
- **API:** `GET /api/commitments?meeting_id=`, `DELETE /api/meetings/<id>`, `POST /api/meetings/clear`, `POST /api/commitments/clear`. The two clear routes need `{"confirm": true}`.

### Added — designation in People, AI-tailored email nudges (`ai_writer.py`)
- **People** have a **Designation** (job title).
  - The CSV template has a Designation column, and "Title", "Job Title", "Role", "Position" and "Grade" headers are recognised.
  - Designations can be edited per person.
  - A CSV without that column keeps the designations already saved.
- **The email channel has a reminder draft:** subject, body, and "Sign as".
  - Placeholders: `{owner_first_name}`, `{owner_name}`, `{designation}`, `{task}`, `{due_date}`, `{status}`, `{meeting}`, `{reason}`, `{sender_name}`.
  - Leaving it blank uses a standard draft.
- **"Let AI tailor each email to the recipient's designation."**
  - AI adjusts tone, formality, length and detail: brief and outcome-focused for executives; clear next steps for individual contributors; more guidance for interns. Overdue reminders are firmer.
  - It keeps every fact and ask and never invents anything.
  - It uses the engine and key saved on the AI extraction page, reading that table directly, so `ai_writer.py` doesn't depend on `ai_extraction.py` and the trial feature can still be reverted.
  - Any AI problem sends the draft as written, and the nudge log says why.
- **"Preview email"** shows the email for a person or designation; nothing is sent.
- Emails sent by "Send test" and by scheduled reminders use the draft. The standard reminder text is still used for Slack, Teams and custom channels.

### Fixed — Integrations / AI extraction didn't show what was saved
- **Cause, nudges:** the channel picker always opened on **Slack**. An account that had saved **Email** saw an empty Slack form and concluded nothing was saved. The page now opens on the saved channel (Email first), shows "✓ Saved — showing your saved … settings", and fills the draft fields.
- **Cause, all settings pages:** pages and API answers had no caching headers, so a browser could reuse an old copy. Every response now carries `Cache-Control: no-cache`. Pages restored by the Back button re-read their data.
- **Cause, failed loads:** one failing request stopped the rest of the page from loading. Each section now loads independently, and any failure is reported.
- **New "Saved right now" summary** at the top of Integrations: nudge channels, automatic nudges, tracker, transcript source, extraction engine and webhook signing, read fresh each time.
- **Unreadable credentials** (encryption key changed) are now flagged on the tracker and transcript forms. They were silently blank before.
- **AI extraction page:** it now shows "✓ Saved: <engine> · model · key ••••abcd · confirmed <date>", and the key field shows its masked saved value.
- The values had been saved correctly all along. On this machine every saved setting decrypts.

### Fixed
- `tests/test_secrets_and_live.py` 5d failed whenever the real pre-encryption backup existed. It now checks that the backup wasn't modified.
- `tests/run_all.py` could crash printing characters the console can't show.

### Tests
- **New suite `src/tests/test_v090_platform.py`: 93 checks.**
  - Sign-up, sign-in, lockout and the pre-0.9.0 account path.
  - Sessions, password change, sign out everywhere, the CSRF guard, cookie flags and security headers.
  - SSO token validation and a full SSO round trip against a stand-in provider.
  - `.docx` and `.pdf` upload, the meeting filter, and clearing.
  - Jobs: async mode, backoff, permanent failure, retry and cancel, crash recovery, and cancelling on delete.
  - Signed webhooks: wrong secret, stale timestamp, replay, tampering, rotation, the retired URL, and the rate limit.
  - Designations, email drafts and AI tailoring.
- Existing suites now sign in with a password and use signed webhooks.
- **Total: 257 checks, all passing.**

### Tried live
On a **copy** of the test database, in Chrome on port 5051:
- Signed in with a password.
- The dashboard opened on the newest meeting and showed only its tasks.
- Uploaded the FY2027 `.docx` and a text PDF. Both were extracted by background jobs: 35 and 26 commitments.
- Integrations showed all saved settings after leaving and coming back.
- The email preview worked.
- The Jobs, People, Account and AI extraction pages all loaded.

**Side effect:** the copy kept the test account's Google Sheets auto-sync, so those two test uploads wrote **61 rows (rows 8–68, IDs 179–239) to the real Google Sheet**. The real database doesn't reference them. Delete them in the sheet if unwanted.

### Docs
- `prd.md` updated (status tables, not built, user stories, roadmap, layout).
- The status diagram was regenerated (13 live / 12 built / 3 proof of concept / 5 not built).
- New `docs/Commitment_Tracker_PRD_v0.9.0.pdf`.
- `readme.md` rewritten for the new layout, sign-in, environment variables, webhooks and API.
- `auth_config.example.json` added.
- `.gitignore` now excludes `flask_secret.key` and `auth_config.json`.

### Known issues
- **Upgrading signs everyone out.** The existing account has no password yet; set one with `python src/manage_users.py set-password you@example.com`, or configure SSO.
- **The Google Sheet → app script must be replaced** with the signed one. Until then, status changes made in the sheet are refused with 410.
- **The first extraction after a start is slow** while TensorFlow and the embedding model load: about 10 minutes on this machine with another load running. It no longer blocks the upload.
- **One app process:** jobs and live updates don't work across several server processes.
- **Not built:** email verification, password reset by email, and MFA.
- `docs/Commitment_Tracker_Technical_Guide.docx` still describes 0.7.3 and the old file layout. Its builder (`src/docs_tools/build_technical_guide.js`) has the new paths but wasn't re-run. Its line references and function inventory need updating.
- `_snapshots/v0.4.0/revert_ai_extraction.py` targets the pre-`src/` layout and the 0.4.0 `app.py`; reverting the AI feature now means removing the marked `[ai-extraction v0.5.0]` blocks by hand.

---

## [0.8.0] — 2026-10-10 — Platform foundations: secret encryption, live dashboard updates

### Security
- **Saved credentials are now encrypted at rest.** The new `secret_store.py` encrypts them with Fernet (from the `cryptography` package, now in requirements.txt). Each value is stored as `enc:v1:…`. Encrypted columns:
  - `nudge_settings.webhook_or_key` and `nudge_settings.config_json`;
  - `tracker_settings.config_json`;
  - `transcript_source_settings.config_json`;
  - `ai_extraction_settings.config_json`.

  `webhook_token` stays readable on purpose: it is the lookup key in webhook URLs, not a stored credential.
- **The encryption key** is read from `COMMITMENT_TRACKER_SECRET_KEY` if that is set. Otherwise it comes from `secret.key`, which is created automatically next to `app.py` and excluded in `.gitignore`. **Back it up.** Without it, every saved credential has to be re-entered. If the key changes:
  - settings pages still load and flag the problem;
  - an action that needs the credential returns 409 and asks you to re-enter it;
  - a scheduled nudge is logged as `failed`.

  Nothing crashes.
- **Existing databases are migrated on start.** `migrate_db()` → `encrypt_stored_secrets()` encrypts any plain-text value it finds. Before the first such migration it copies the database to `_snapshots/v0.7.3/commitment_tracker.db.bak`. That copy still holds the credentials in plain text, so delete it once you've confirmed the app works.
- **Fixed (0.7.3 known issue):** `GET /api/tracker-settings` and `GET /api/transcript-source-settings` no longer return stored secrets. Secret fields come back blank, plus a masked `secret_hints` entry (`••••abcd`). Saving a secret field blank keeps the stored value, but only when the provider hasn't changed. The Integrations page shows "Saved (••••abcd) — leave blank to keep" in those fields.

### Added
- **Live dashboard updates (Server-Sent Events).** `GET /api/events` streams changes for the signed-in account (`live_events.py` is the in-process pub/sub). After committing a change, routes call `notify()`. Events are published only when the response succeeds:

  | Event | Raised when |
  |---|---|
  | `meeting_created` | Upload, generic or Fathom webhook, simulated capture, demo data |
  | `commitments_changed` | A commitment is added, edited, extracted or synced, or a people-directory change re-matches owners |
  | `commitment_status` | A status change arrives from the tracker (Sheet/Jira webhook, or the simulate button) |
  | `nudge_sent` | Any nudge, including ones sent by the scheduler thread |

  - The dashboard reloads within ~0.3 s of an event. It shows a toast for status changes from the tracker, refreshes an open commitment's detail, and shows a Live / Reconnecting… indicator. It falls back to the previous 8-second polling if the stream is unavailable.
  - The Integrations page's nudge and transcript activity logs also update live.
  - Limit: one app process. Several worker processes would need a shared broker such as Redis.
- `tests/test_secrets_and_live.py`: 41 checks covering encryption, masking, keep-on-blank, migration, a wrong key, event routing and isolation per account, and the SSE stream itself. `tests/_support.py` now gives every suite a throwaway key. Total: 160 checks, all passing.

### Changed
- `docs/make_status_diagram.py` / `docs/status-diagram.svg`:
  - "Secret encryption" is now live;
  - a new "Live updates" foundation is added (live);
  - totals are now 9 live / 9 built / 4 proof of concept / 9 not built.
- `prd.md` is updated to match. The PRD PDF has **not** been rebuilt yet; run `python docs/build_prd_pdf.py`.
- The `app.py` module docstring no longer describes nudges and Sheets as mocks.

### Fixed
- `POST /api/commitments/<id>/sync-tracker` released its database write lock only on the success path. A failure to decrypt the credential would have left the database locked.

### How to revert
1. Restore `_snapshots/v0.7.3/commitment_tracker.db.bak` over `commitment_tracker.db`.
2. Check out the 0.7.3 versions of `app.py`, `ai_extraction.py` and `static/`.
3. Remove `secret_store.py` and `live_events.py`.

---

## [0.7.3] — 2026-10-02 — Security fix, test suite in the repo, technical handover guide

### Security
- **Fixed script injection (XSS) in the dashboard.** The task-tracker sync history was inserted into the page without HTML escaping. Part of that text comes from the `provider` field of the unauthenticated `POST /api/webhooks/tracker-update`, so an outside caller who knew an account's email and a row reference (e.g. `row-2`) could run script in that user's dashboard. It is now escaped.
- `.gitignore` now excludes the recordings of real meetings and lectures, because they name real participants. These are the two AIPL-BM4 transcripts and `fraud_ai_implementation_meeting_transcript.txt`.
  - It also excludes the two `training_data/*.csv` files, which copy the lecture's lines verbatim.
  - All of these stay on this machine only. To rebuild the labelled dataset, rerun `ml/prepare_transcript_dataset.py` here.
- The same missing escaping was fixed on the Integrations page. These spots could only be reached by the user's own input or by AI-generated text: AI-suggested setup steps and their disclaimer, and the tracker and transcript-source summaries.

### Added
- **`tests/`** — the test suites written during 0.3.1–0.7.0, previously kept only outside the repo, plus a new parsing and validation suite:

  | File | Checks |
  |---|---|
  | `test_transcript_parsing.py` | 28 |
  | `test_sheets_sync.py` | 15 |
  | `test_people_and_nudges.py` | 54 |
  | `test_ai_extraction.py` | 22 |

  Run them all with `python tests/run_all.py` (119 checks, all passing). Each suite uses a temporary database and fake external services, so it never touches `commitment_tracker.db`, the real sheet, or sends real messages. `tests/fixtures/schema_v0.5.0.sql` supports the database-upgrade test.
- **`docs/Commitment_Tracker_Technical_Guide.docx`** — a 55-page handover guide for engineers new to the codebase. It covers:
  - code flow;
  - every function, class and route;
  - encryption;
  - chunking;
  - databases;
  - credentials;
  - security measures and gaps;
  - edge cases and testing;
  - an ML primer;
  - a debugging runbook;
  - working conventions;
  - the backlog.

  It includes 11 diagrams on landscape pages and a worked example from a real run of the model.
- **`docs/make_guide_diagrams.py`** — draws the guide's diagrams as SVG and renders them to PNG with headless Edge (output: `docs/guide_diagrams/`).
- **`docs/build_technical_guide.js`** — builds the guide's .docx with the `docx` npm package.
  - It takes an AST inventory of the Python code.
  - The build fails if any function has no description, so the reference stays complete.

### Known issues (documented, not yet fixed)
- `GET /api/tracker-settings` and `GET /api/transcript-source-settings` return stored secrets in full (Jira API token, Zoom client secret, Fathom webhook secret). The nudge and AI settings endpoints already mask theirs.
- The Fathom signature check doesn't reject old timestamps, so a captured webhook could be replayed.
- `app.secret_key` is hard-coded in source, and `app.run(debug=True)` enables the Werkzeug debugger. Both are acceptable only while the app stays on localhost; exposing it through ngrok exposes them too.
- Found while writing the guide:
  - Built-in extraction runs inside the upload's open write transaction, so the database stays write-locked during ML inference.
  - `due_of` uses only the first due phrase, so "on the 30th … by Friday" ends up with no due date.
  - Task text is cut at 120 characters, even mid-word.
  - The module docstring at the top of `app.py` still describes nudges and Sheets as mocks.

---

## [0.7.2] — 2026-10-02 — Status diagram and shareable PRD PDF

### Added
- **Status diagram** (`docs/status-diagram.svg`) embedded in prd.md under "Current status → Status at a glance". It shows the product loop (Capture → Extract → Assign → Track & sync → Nudge) plus the platform foundations. Each piece is colour-coded live / built (first live run pending) / proof of concept / not built. Totals: 7 / 9 / 4 / 10. Regenerate with `python docs/make_status_diagram.py`; the statuses are listed at the top of that script.
- **PRD as PDF** (`docs/Commitment_Tracker_PRD_v0.7.2.pdf`, 5 pages, diagram on its own landscape page) for sharing with the team. Rebuild with `python docs/build_prd_pdf.py`. It renders prd.md through Microsoft Edge's print-to-PDF and needs `pip install markdown` (a docs-only dependency, not added to requirements.txt).

---

## [0.7.1] — 2026-10-02 — Documentation brought up to date

### Changed
- **prd.md rewritten** to match the product as built. Its new "Current status" section lists:
  - what's built with a real integration, and whether each part was verified live or only against stand-ins;
  - what's a proof of concept with no real integration;
  - what isn't built.

  It also adds sections on data and ML status and on source control, and updates the open questions and the roadmap. The previous version still described nudges and Google Sheets sync as mocked, and the parser as "in progress elsewhere".
- **readme.md** "Status" and "Known limitations" sections corrected and pointed at the PRD. They still claimed nudges and outbound sync were simulated.
- No code changes.

---

## [0.7.0] — 2026-10-01 — Retraining pipeline on the team's transcripts; transcript parsing fixes

### Fixed (live)
- **"[09:00] Name: text" transcripts were read as one speaker.** A line with the timestamp, speaker and words together is the most common export format, and all the team's generated transcripts use it, but it wasn't recognized. Whole meetings collapsed into two or three "turns" owned by header lines such as "Format". On `01_ai_customer_service.txt` the result went from 3 turns to 69 turns across the 7 real participants.
- **WebVTT/SRT captions** (cue numbers, `00:12:31.020 --> 00:12:31.710` timing lines, and the speaker on the timing line) are now read correctly.
- **Curly apostrophes** (`I’ll`), as written by Word and Zoom, broke the "I'll / I will" owner detection. Text is now normalized to plain quotes first.
- **Section headings** such as "ACTION ITEMS" or "OPEN QUESTIONS" now start a written-notes section. Previously, list items were credited to whoever spoke last.
- More preamble fields are ignored as speakers: Meeting ID, Format, Facilitator, Purpose, Context and similar.

### Added
- `ml/prepare_transcript_dataset.py` — splits transcripts into lines exactly as the app does, groups files into families, and removes near-duplicates. Writes `training_data/utterances_for_labelling.csv`.
- `training_data/labels.py` and `training_data/labelled_utterances.csv` — the first-pass labels, with the definition used and a list of borderline calls, for the team to review. 2,996 distinct lines, 158 commitments.
- `ml/retrain_with_transcripts.py` — retrains on the original synthetic CSV plus the labelled lines, and evaluates leave-one-family-out: each family is scored by a model that never saw it, against the current model. Results are in `ml/retrain_report.json`.
- `ml/action_detector_candidate.keras` — the retrained model. **Not deployed** (see below).

### Training outcome (2026-10-01)

| Held-out family | Lines | Real commitments | Current model F1 (precision / recall) | Retrained F1 (precision / recall) |
|---|---|---|---|---|
| template_numbered (10 files, 1 template) | 302 | 22 | 0.25 (0.15 / 0.68) | 0.63 (0.55 / 0.73) |
| template_corpus (180 "meetings", 1 template) | 390 | 81 | 0.28 (0.22 / 0.37) | 0.45 (0.48 / 0.42) |
| workshop_clustering (AI rewrite) | 234 | 12 | 0.19 (0.12 / 0.58) | 0.50 (1.00 / 0.33) |
| workshop_validation (AI rewrite) | 152 | 11 | 0.26 (0.15 / 1.00) | 0.64 (0.57 / 0.73) |
| real_lecture (real recording, a class) | 1,809 | 19 | 0.03 (0.01 / 0.26) | 0.03 (0.02 / 0.05) |
| fy2027 (business meeting) | 109 | 13 | **0.42** (0.29 / 0.77) | 0.38 (0.50 / 0.31) |
| **Average across families** | | | **0.24** | **0.44** |

- The retrained model raises false-positive precision everywhere. For example, it flags 51 lecture lines instead of 353, and 8 FY2027 lines instead of 35.
- It also misses more real commitments: 4 of FY2027's 13, versus 10 before.
- **Why it was not deployed.** The script deploys on an improved average, and did, but that was reverted. The average is driven by the synthetic template families. The one business meeting closest to real use, FY2027, got worse, and the only real recording is a lecture rather than a meeting. Production keeps the previous model (verified byte-identical to `_snapshots/v0.6.0/ml/action_detector.keras`).

### Data flags
- The 10 numbered files are one template with names and topics swapped: 640 lines, 124 distinct sentences, and impossible timestamps such as `[09:61]`.
- `large_synthetic_meeting_transcript.txt` is 180 meetings and about 1M words, but only **273 distinct sentences**.
- The only real recording (`fraud_ai_implementation_meeting_transcript.txt`) is a class lecture with the names swapped, not a business meeting. Its line breaks were also lost, so it can't be uploaded through the app as-is.
- The two `.docx` files are the only genuinely different generated meetings, about 5,600 words in total. The app accepts `.txt` only.
- In effect there are ~5 independent sources, ~158 labelled commitments (72 of them one templated sentence), and **zero real business meetings**. That's not enough to retrain reliably, or to measure a retrained model reliably.

### Snapshot
- `_snapshots/v0.6.0/` holds `transcript_parser.py`, `ml/action_detector.keras` and `ml/train_action_detector.py` from before this version.

---

## [0.6.0] — 2026-09-27 — People directory and real nudges

### Added
- **People page** (`/people`, new sidebar link). Upload a CSV with the columns First Name, Last Name, Email ID and Corporate User Name. Only Email ID is required, and common header spellings are accepted. It can **merge** into the existing list or **replace** it. People can also be added, edited and removed one at a time. Invalid emails and duplicate rows are reported per row, and a template CSV is available to download.
- **Automatic owner tagging.** Every new task's owner name is matched against the directory the moment the task is created. This covers manual upload, Fathom, re-extraction, the AI engine and hand-entered tasks. Matching is by corporate user name or email, then full name (ignoring middle names), then a single first or last name.
  - **Shared names are never guessed.** If two people fit (e.g. two Kevins), the task is marked "shared name — pick email" and the matching people are suggested.
  - **Manual override.** A new "Owner email" field on each task accepts any email, with suggestions from the directory. A hand-set email is never overwritten by automatic matching. Clearing the field hands the task back to automatic matching.
  - **Back-fill.** Changing the directory re-checks existing tasks that were matched automatically or not matched yet, so uploading the directory after the meetings works too.
- **Real nudges** — until now, nudges were only logged:
  - **Email** through any SMTP server: Gmail with an app password (a "Fill in Gmail settings" button is provided), Microsoft 365, or services such as SendGrid.
  - **Slack:** a bot token (`xoxb-…`) sends each owner a direct message, found by their email. An incoming-webhook URL posts in one channel instead.
  - **Teams:** a Workflows (Power Automate) webhook URL. It receives an Adaptive Card that the standard channel workflow displays, plus `recipient_email` and `text` fields for a flow that messages the owner directly.
  - **Custom:** JSON with the recipient, task, due date, status and message is POSTed to any URL.
- **Send nudge now** on each task, choosing any configured channel. **Send test** on each saved channel sends a sample reminder to your own address. Saved channels can be removed.
- **Automatic nudges** (off by default). When switched on, due reminders are checked every 15 minutes while the app runs, following each channel's cadence:
  - "1 day before due" and "On the due date" send once per task and channel.
  - "Daily once overdue" sends once a day until the task is done.
  - Finished tasks are never nudged.
  - "Send due reminders now" runs the same check immediately. The interval can be changed with `NUDGE_CHECK_INTERVAL_SECONDS`.
- Nudge history shows the real outcome — sent, failed or skipped — with the reason, e.g. "the task owner has no email". Recent nudges are listed on the Integrations page.
- Endpoints: `GET /people`; `GET|POST /api/people`, `PATCH|DELETE /api/people/<id>`, `POST /api/people/upload`, `GET /api/people/template.csv`; `DELETE /api/nudge-settings/<channel>`, `POST /api/nudge-settings/<channel>/test`, `GET|POST /api/nudge-preferences`, `POST /api/nudges/run-now`, `GET /api/nudges/log`.
- New modules: `people_directory.py` (CSV parsing and name matching) and `notifications.py` (delivery; Python standard library only, no new dependencies).

### Changed
- `POST /api/commitments/<id>/send-test-nudge` now really sends over the requested channel. It returns `status` (`sent` / `failed` / `skipped`) and `detail`.
- `PATCH /api/commitments/<id>` also accepts `owner_email`. Status-only updates behave and respond exactly as before.
- Task details include `owner_match` (`auto` / `manual` / `ambiguous` / `none`) and `owner_candidates`. The dashboard table shows each owner's email under their name.
- Nudge channel settings: email takes SMTP details instead of an "API key". Saved webhook URLs, tokens and passwords are no longer sent back to the browser in full, and a blank field keeps the saved value.
- Database: new `people` and `nudge_preferences` tables; new columns `commitments.owner_match`, `nudge_settings.config_json`, `nudge_log.reason` and `nudge_log.detail`. Existing databases are upgraded automatically on start, with existing data kept. `migrate_db()` now also creates any new tables.
- The 0.5.0 revert script (`_snapshots/v0.4.0/revert_ai_extraction.py`) now removes only the lines between its `[ai-extraction v0.5.0]` … `[/ai-extraction v0.5.0]` markers, instead of restoring whole files. It can therefore still be used after later versions without undoing their changes. End markers were added to the 0.5.0 blocks for this.

### Known issues
- Teams messages need a Microsoft 365 work or school account, because personal Teams has no Workflows. Slack direct messages need the owner's Slack email to match the directory.
- Automatic nudges only run while the app is running on this machine; a hosted deployment would need a real scheduler.
- SMTP passwords and tokens are stored unencrypted in the local database, like the other integration secrets.
- Channels saved before 0.6.0, when nudges were mock-only, now send for real when used. Check the saved channels, and remove any placeholder values, before switching on automatic nudges.
- Task extraction quality is unchanged in this version. The built-in model was trained only on synthetic, templated meetings. On the real FY2027 transcript, roughly a third of the 35 items it flagged were real commitments. Owners in the read-out assignment list ("Kevin Heys (Due: September 27): …") were attributed to the reader, and "Due: September 26" dates weren't recognized. Improving this needs rule fixes and/or retraining on real, labeled transcripts; not started yet.

---

## [0.5.0] — 2026-09-24 — Optional AI extraction engine (isolated, revertible)

Built as a separate, switchable feature pending team review. Everything from 0.4.0 works exactly as before for any account that doesn't opt in.

### Added
- **New "AI extraction" page** (sidebar link, `/ai-extraction`). Each account chooses what extracts commitments from its transcripts:
  - **Built-in model** — the default and the existing behavior.
  - **Claude (Anthropic API)** with the customer's own API key; model defaults to `claude-opus-5`. Uses structured JSON output, and for Claude Opus 5 / Fable 5.1 opts into Anthropic's server-side fallback, so a false-positive safety decline doesn't lose the extraction.
  - **OpenAI-compatible API** with the customer's own key — OpenAI, Azure OpenAI, Google Gemini, Groq, or a local Ollama server (free, no key). Provider presets fill in the base URL.
- **Safe fallback.** If the AI engine fails for any reason (bad key, rate limit, network, invalid reply), that meeting is extracted by the built-in model instead, and the failure is listed under "Recent AI extraction runs". Uploads, live captures and Google Sheets auto-sync are never blocked by it.
- **Consent gate.** An AI engine can only be saved after confirming that transcripts may be sent to that provider.
- **Test connection** — runs the saved engine on a four-line sample transcript.
- **Side-by-side comparison** — runs both engines on any meeting's saved transcript without changing the meeting. Each item can be marked "real commitment" or "not one", and a **scoreboard** shows each engine's precision across all reviews, to support the team's decision with evidence from real meetings.
- `GET /api/ai-extraction/status`, `GET|POST /api/ai-extraction/settings`, `POST /api/ai-extraction/test`, `GET /api/ai-extraction/runs`, `POST /api/ai-extraction/compare`, `POST /api/ai-extraction/comparisons/<id>/verdicts`, `GET /api/ai-extraction/scoreboard`.
- New dependency: `anthropic` (the official Anthropic Python SDK). It's only imported when the Claude engine is used.

### How it is isolated
- New files only: `ai_extraction.py` (all logic and routes) and `static/ai-extraction.html` (the page).
- Its own database tables — `ai_extraction_settings`, `ai_extraction_runs`, `ai_extraction_comparisons` — created on first use. No existing table was altered.
- Existing files changed only by clearly marked `[ai-extraction v0.5.0]` blocks:
  - `app.py`: a 4-line hook at the top of each of the two extraction helpers, which returns to the unchanged built-in path whenever the account uses the built-in engine or the AI call fails; one registration block at the end of the file; and `APP_VERSION`.
  - `static/app.js`: adds the sidebar link only when the feature is on.
  - `requirements.txt`: `anthropic`.
- Verified: the Google Sheets and auto-sync test suites from 0.3.1/0.4.0 produce identical results with the feature loaded, and upload responses have the same fields as before.

### How to revert
- **Switch off without changing any file:** start the app with the environment variable `AI_EXTRACTION_ENABLED=0` (PowerShell: `$env:AI_EXTRACTION_ENABLED="0"; python app.py`). The page, routes and link disappear, and extraction behaves exactly as in 0.4.0.
- **Remove it entirely:** `python _snapshots/v0.4.0/revert_ai_extraction.py` (a dry run that shows what will change), then the same command with `--yes`. This restores `app.py`, `requirements.txt` and `static/app.js` from the snapshot taken just before this version (verified byte-identical), and deletes the two new files. Add `--drop-tables` to also delete the feature's database tables. The dry run warns if any of those files was edited after 0.5.0, since restoring would undo those edits too.

### Known issues
- API keys are stored unencrypted in the local SQLite database, like every other integration key in the app. They are never sent back to the browser, which only shows the last four characters. Encryption is needed before real customers use this.
- AI extraction runs inside the upload request, so an upload using an AI engine can take up to a minute.
- Very long transcripts are sent in a single request. If a model's reply is cut off, that meeting falls back to the built-in model.
- Scoreboard precision only counts false positives. Commitments an engine missed are visible in the side-by-side view but not scored.

---

## [0.4.0] — 2026-09-24 — Automatic sync to Google Sheets

### Added
- **Auto-sync to Google Sheets.** New "Sync automatically" option under Integrations → Task tracker → Google Sheets. When on, every new commitment (manual upload, Fathom capture, re-extraction, or added by hand) is written to the sheet as soon as it's created; a whole meeting's commitments are written in a single Sheets API call.
- **In-app status changes update the sheet.** With auto-sync on, changing a commitment's status in the app rewrites the Status cell (column E) of its sheet row.
- **"Sync all to sheet" button** on the dashboard and `POST /api/tracker/sync-all` — writes every not-yet-synced commitment in one call. Works with auto-sync on or off (used to catch up a backlog).
- Upload/capture/re-extract API responses now include `synced_to_tracker` and `tracker_sync_error`; the dashboard toast reports whether commitments reached the sheet.

### Changed
- "Sync to tracker" on a commitment that is already in the sheet now updates its status cell instead of doing nothing.
- `google_sheets.py` writes rows in batches (`append_commitment_rows`) and can update a single status cell (`update_status`).
- The Integrations tracker summary shows whether auto-sync is on.

### Known issues
- Existing Google Sheets settings saved before 0.4.0 count as auto-sync **off** until the tracker settings are saved again.
- The built-in classifier flags many ordinary discussion lines as commitments (35 in the FY2027 sample meeting), so auto-sync can write noisy rows to the sheet.

---

## [0.3.1] — 2026-09-24 — Google Sheets two-way sync fixes

### Fixed
- **Rows landing in the wrong columns.** The Sheets "append" call guesses where the table is; an empty cell (e.g. no due date) made it treat column E as a new table, so rows were written to E:I and the status ended up in column I — where the Apps Script (watching column E) never saw edits. Rows are now written to an explicit `A<n>:E<n>` range, with a header row (ID, Task, Owner, Due date, Status) added to an empty sheet.
- **Duplicate rows from a double-click** on "Sync to tracker". The sync now takes the database write lock before checking whether the commitment is already synced, so a second request waits and sees the first one's result. The button is also disabled while a sync is running.
- **Status words other than pending / in_progress / done were rejected.** Inbound updates now accept common synonyms: Open, To do, Not started → pending; In progress, Started, Doing, WIP → in progress; Done, Closed, Complete(d), Resolved, Finished → done. Case and spaces don't matter.
- The "Mock only — no request actually sent" label next to Sync to tracker was wrong for Google Sheets; it now says the sync writes a real row.

### Changed
- Statuses are written to the sheet as readable labels ("Pending", "In progress", "Done").
- Updated Apps Script for the Sheet → App flow: skips the header row, sends the status as typed, adds the `ngrok-skip-browser-warning` header, logs the response, and includes a `testStatusEdit()` helper that can be run by hand from the editor.
- Database connections wait up to 30 seconds for a lock instead of 5.

---

## [0.3.0] — 2026-09-24 — Real Fathom and Google Sheets integrations

### Added
- **Fathom as a transcript source** (free plan; its bot joins Zoom and Google Meet calls). New `POST /api/webhooks/fathom/<token>` endpoint verifies Fathom's HMAC-SHA256 webhook signature, reads the transcript Fathom sends already split into speaker turns, creates the meeting, and runs commitment extraction. New "Fathom" option and setup steps under Integrations → Transcript source.
- `extract_commitments_from_turns()` in `transcript_parser.py` — extraction from speaker/text pairs supplied by a structured source, skipping the raw-text parsing.
- **Real Google Sheets sync (App → Sheet).** "Sync to tracker" writes an actual row via the Google Sheets API instead of logging a mock message. Jira and Custom trackers remain mocked.
- `google_sheets.py` (Sheets API calls) and `google_oauth_setup.py` (one-time browser login that saves a reusable token), using an OAuth Client ID file from Google Cloud.
- Ready-to-paste Google Apps Script on the Integrations page for the Sheet → App status flow (installable On edit trigger calling `POST /api/webhooks/tracker-update`).
- New dependencies: `google-api-python-client`, `google-auth-oauthlib`, `google-auth-httplib2`.

### Changed
- Google Sheets tracker settings need only the spreadsheet ID (the API key field was removed; authentication comes from the OAuth token).
- The live-capture webhook box on the Integrations page shows the Fathom-specific URL when Fathom is selected.
- `tracker_sync_log.status` gains a `synced` value for real (non-mock) syncs.

### Security
- `client_secret*.json` and `google_token.json` added to `.gitignore` so Google credentials can't be committed.

---

## [0.2.2] — 2026-09-23 — Transcript upload and parsing fixes

### Fixed
- **Garbled commitments after uploading a .docx.** The upload form only hinted `.txt`, so a Word file was read as raw text and produced unreadable "commitments". The dashboard now rejects non-.txt files with a clear message, and `POST /api/meetings` and the generic capture webhook reject content that looks like a binary file.
- **Every commitment attributed to the wrong owner** for transcripts using the common `[10:00 AM] Name:` speaker format (Otter / Teams / Zoom exports). That format wasn't recognized, so the whole transcript collapsed into one turn owned by a header line. Added support for it, and header fields such as `Date:`, `Participants:` and `Meeting Transcript:` are no longer mistaken for speakers.

### Changed
- Removed stale "no parser is wired in yet" wording from the upload endpoint's description.

---

## [0.2.1] — 2026-09-23 — Logic ported from the updated Colab notebook

### Fixed
- Speaker labels that are only digits (e.g. a stray timestamp fragment like `12:34 - 12`) are treated as an unknown speaker instead of a person named "12". Ported from section 5 of the updated `Meeting_Transcript_Definition.py`.

---

## [0.2.0] — 2026-09-21 — ML commitment extraction

### Added
- **Automatic commitment extraction** from transcripts, ported from the Colab prototype (`Meeting_Transcript_Definition.py`):
  - `transcript_parser.py` — splits a transcript into speaker turns and sentences, classifies each sentence as a commitment or not, and pulls out owner, task and due date.
  - `ml/train_action_detector.py` — trains the classifier (sentence embeddings + small neural network) from `train_utterances_balanced.csv` / `test_utterances_balanced.csv` and saves `ml/action_detector.keras`.
  - Due-date phrases ("Thursday", "tomorrow", "this week", "EOD", …) are converted to real dates relative to the meeting date.
- Extraction runs automatically on manual upload, the live-capture webhook, and the "Simulate incoming transcript" button; extracted commitments carry a confidence score.
- `POST /api/meetings/<id>/extract-commitments` and an "Extract commitments" button in the meeting view to re-run extraction.
- `requirements.txt` (TensorFlow, sentence-transformers, spaCy, pandas, scikit-learn, pypdf).

### Changed
- The app still starts with only Flask installed; if the ML libraries or the trained model are missing, uploads succeed and report that extraction was unavailable.
- Upload and meeting-view wording updated to reflect automatic extraction.
- readme, PRD and requirements documents updated for the new parser.

### Fixed
- Re-running extraction on the same meeting no longer creates duplicate commitments (skips sentences that already have one).

---

## [0.1.0] — 2026-08-22 to 2026-09-03 — Prototype (committed to git)

Git commits `b92fcc9`, `1d78966`, `0b5e703`, `1ad95f9`.

### Added
- Flask + SQLite prototype: email-only sign-in, dashboard with meetings, filterable commitments table, overdue flags and status chart; light/dark theme; demo data.
- Manual transcript upload and hand-entered commitments.
- Integrations page: nudge channels (mocked), task tracker — Jira / Google Sheets / custom (outbound mocked), transcript sources — Zoom / Google Meet / Teams / Otter / custom (mocked "check now").
- Real inbound tracker webhook (`POST /api/webhooks/tracker-update`) with a simulate button.
- Custom-app option with AI-suggested setup steps.
- Real inbound transcript-capture webhook with a per-user token, and live dashboard polling for newly captured meetings.
