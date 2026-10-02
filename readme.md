# Commitment Tracker

A local prototype dashboard that turns meeting commitments ("I'll get that done by Friday") into tracked, nudge-able tasks. Built to validate the UX and product concept before committing to a hosting platform or wiring up real integrations.

Commitment extraction is real: a transcript (uploaded by hand, or pushed in via a live-capture webhook) is run through a trained ACTION/DIALOG classifier (`transcript_parser.py` + `ml/train_action_detector.py`, originally prototyped in a Google Colab notebook) that finds commitment-shaped utterances and pulls out the task, owner, and due date. This dashboard is where a user goes to track, follow up on, and optionally sync those commitments to wherever their team already works.

**Version:** see `APP_VERSION` in [app.py](app.py) (also shown in the sidebar and at `GET /api/version`). Every change is recorded in [CHANGELOG.md](CHANGELOG.md).

## Status

Local prototype, free to use, email-only sign-in. Real integrations: commitment extraction (built-in model or the customer's own AI), Fathom transcript capture, two-way Google Sheets sync, people directory with owner tagging, and nudges by email / Slack / Teams / custom webhook. Still simulated: Jira and custom-tracker outbound sync, and "check now" pulls from Zoom / Google Meet / Teams / Otter.

**The up-to-date picture of what is real, what is simulated, and what isn't built is in the "Current status" section of [prd.md](prd.md).** Some feature descriptions below were written for earlier versions; where they disagree with the PRD or [CHANGELOG.md](CHANGELOG.md), those two are correct.

## Features

- **Landing page** with a sample "extracted from your meeting" card and an email-only "Get started" form (no password, no OAuth).
- **Dashboard**: meetings list, a filterable commitments table (status / owner), overdue flagging, and a compact status-breakdown chart (Pending / In Progress / Done).
- **Manual transcript upload**: paste or upload a `.txt` transcript to create a meeting — commitments are extracted automatically (see below); add or correct any by hand from the meeting detail view. This path is fully real — nothing here is mocked.
- **Commitment detail modal**: source quote, owner, due date, extraction confidence (blank for hand-entered commitments), status control, nudge history, and task-tracker sync history (both directions).
- **Integrations page**:
  - *Nudges* — per-user Slack / Teams / email / custom webhook or API key, cadence (day-before / on-due / daily-once-overdue). "Send test nudge" logs an entry; nothing is sent. Each channel has "how to get these details" steps.
  - *Task tracker* — connect Jira, Google Sheets, or a custom app (or neither), with setup steps for each. Outbound "Sync to tracker" logs what *would* be sent and mints an `external_ref` (e.g. a fake Jira issue key). See two-way sync below.
  - *Transcript source* — configure which app (Zoom / Google Meet / Microsoft Teams / Otter.ai / custom) transcripts would come from, with setup steps for each. "Check for new transcripts" is still a mocked pull (logs what would happen); the live-capture webhook path below is real, and whatever transcript text arrives through either path gets extracted the same way. Not configuring a source is fine; upload transcripts by hand instead.
  - *Custom apps* — if a provider isn't in the predefined list, pick "Custom" in any of the three sections above and name your own app. Its "how to get these details" steps aren't hand-written; they're suggested by a live AI call (`/api/setup-steps/suggest`) and always shown with a disclaimer that they're unverified. Set `ANTHROPIC_API_KEY` as an environment variable to enable the real call — without it, a generic template list is shown instead (still disclaimed).
- **Two-way tracker sync**: outbound push is mocked (logged only), but `POST /api/webhooks/tracker-update` is a *real, functional* inbound endpoint — point a Jira Automation rule or a Google Apps Script trigger at it and a status change made there updates the matching commitment here. No outbound call is needed to receive a webhook, so this half is safe to make real even while push stays mocked. A "Simulate update from tracker" button in the commitment modal exercises the same path without needing a live external caller.
- **Commitment extraction**: `POST /api/meetings` (manual upload), `POST /api/webhooks/transcript-capture/<token>` (real live-capture push), and `simulate-incoming-capture` all run the saved transcript text through `transcript_parser.extract_commitments` — a sentence-embedding classifier (trained by `ml/train_action_detector.py`) flags commitment-shaped utterances, then regex heuristics pull out the owner, task text, and (best-effort) due date. Extracted commitments carry a `confidence_score`; hand-entered ones don't. A meeting still saves even if the model hasn't been trained yet — it just arrives with no auto-extracted commitments, and `POST /api/meetings/<id>/extract-commitments` (also a button in the meeting detail view) can backfill or re-run extraction later.
- **Light/dark theme** toggle, persisted per browser, no flash of the wrong theme on load.
- **Demo data**: a "Load demo meeting" button seeds two sample meetings with realistic commitments so the app isn't empty on first look.

## Tech stack

Python (Flask) + SQLite + static HTML/CSS/vanilla JS for the app itself — no build step, no frontend framework, no cloud account required. Commitment extraction adds a small ML stack (TensorFlow/Keras + sentence-transformers + spaCy) that runs locally, no external API calls.

- `app.py` — Flask app and all API routes
- `schema.sql` — SQLite schema
- `static/` — the three pages (`index.html`, `dashboard.html`, `settings.html`), shared `app.js`, and `style.css`
- `transcript_parser.py` — turn/utterance splitting, the ACTION classifier inference call, and owner/task/due-date heuristics
- `ml/train_action_detector.py` — one-time training script that produces `ml/action_detector.keras` from `train_utterances_balanced.csv` / `test_utterances_balanced.csv`

## Running it

```bash
pip install -r requirements.txt
python -m spacy download en_core_web_sm
python ml/train_action_detector.py   # one-time: trains ml/action_detector.keras
python app.py
```

Open `http://localhost:5000`, enter any email address, and click **Load demo meeting** to populate sample data (or **Upload transcript** to run a real transcript through extraction).

Skipping the training step is fine — the app still runs (`pip install flask` + `python app.py` is enough to click around), it just won't auto-extract commitments from uploaded transcripts until the model exists.

Optionally, set `ANTHROPIC_API_KEY` before running to enable real AI-suggested setup steps for custom (user-defined) apps in Integrations — the app runs fine without it, falling back to a generic template.

## API overview

All routes except `/`, `/dashboard`, `/settings`, `/api/identify`, and the webhook below require an active session (set via `POST /api/identify`).

| Route | Method | Purpose |
|---|---|---|
| `/api/identify` | POST | Upsert a user by email, start a session |
| `/api/me` / `/api/logout` | GET / POST | Session check / end |
| `/api/meetings` | GET / POST | List meetings / manually create one (transcript upload, auto-extracts commitments) |
| `/api/meetings/<id>` | GET | Meeting detail, incl. its commitments |
| `/api/meetings/<id>/extract-commitments` | POST | (Re)run extraction over a meeting's saved transcript |
| `/api/meetings/seed-demo` | POST | Insert demo meetings + commitments |
| `/api/commitments` | GET / POST | List (filter by `status`, `owner`) / manually add one to a meeting |
| `/api/commitments/<id>` | GET / PATCH | Detail (incl. nudge/tracker history) / update status |
| `/api/commitments/<id>/send-test-nudge` | POST | Mock nudge — logs only |
| `/api/commitments/<id>/sync-tracker` | POST | Outbound tracker sync for one commitment — real for Google Sheets (writes the row, or updates its status if already synced), mocked for Jira/Custom |
| `/api/tracker/sync-all` | POST | Writes every not-yet-synced commitment to the Google Sheet in one API call |
| `/api/commitments/<id>/simulate-inbound-update` | POST | UI test button for the inbound sync path (session-authed) |
| `/api/webhooks/tracker-update` | POST | **Real** inbound endpoint — a Jira/Sheets automation posts here |
| `/api/nudge-settings` | GET / POST | List / upsert a nudge channel |
| `/api/tracker-settings` | GET / POST | Get / upsert the user's task tracker config |
| `/api/transcript-source-settings` | GET / POST | Get / upsert which app transcripts come from |
| `/api/transcript-source/check-now` | POST | Mock "check for new transcripts" — logs only |
| `/api/transcript-source/log` | GET | Recent transcript-source check log |
| `/api/setup-steps/suggest` | POST | AI-suggested (or template-fallback) setup steps for a custom app |

## Known limitations (by design, for now)

The full, current list is in [prd.md](prd.md) under "Not built" and "Data & ML status". The most important:

- No real authentication — anyone who knows a user's email can "sign in" as them.
- The tracker-update webhook trusts an email plus a row reference; stored secrets (API keys, SMTP password, tokens) are not encrypted.
- Extraction quality on real meetings is limited until the model is retrained on real business-meeting transcripts.
- Inbound webhooks need a public URL (ngrok today); automatic nudges only run while the app is running.
- SQLite + Flask's dev server — not meant to run in production as-is.

## What's next

See the **Roadmap** section of [prd.md](prd.md).
