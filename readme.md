# Commitment Tracker

A local prototype dashboard that turns meeting commitments ("I'll get that done by Friday") into tracked, nudge-able tasks. Built to validate the UX and product concept before committing to a hosting platform or wiring up real integrations.

This app is the **tracking half** of a larger product idea: a transcript-parsing engine (being built separately, currently in a Google Colab notebook, expected to move into this repo in the near term) extracts commitments from a meeting, and this dashboard is where a user goes to track, follow up on, and optionally sync those commitments to wherever their team already works.

## Status

Prototype. Free to use, no account system beyond an email address, no payment anywhere. Most external integrations — nudges (Slack/Teams/email), transcript sourcing (Zoom/Google Meet/Teams/Otter.ai), and the *outbound* half of task-tracker sync (Jira/Google Sheets) — are **simulated**: the settings UI and data model are real, but no outbound network request is made to any of those services. The *inbound* half of tracker sync is a real exception — see "Two-way tracker sync" below. See [prd.md](prd.md) and [requirement.md](requirement.md) for the reasoning.

## Features

- **Landing page** with a sample "extracted from your meeting" card and an email-only "Get started" form (no password, no OAuth).
- **Dashboard**: meetings list, a filterable commitments table (status / owner), overdue flagging, and a compact status-breakdown chart (Pending / In Progress / Done).
- **Manual transcript upload**: since the real parser isn't wired in yet, paste or upload a `.txt` transcript to create a meeting, then add its commitments by hand from the meeting detail view. This path is fully real — nothing here is mocked.
- **Commitment detail modal**: source quote, owner, due date, extraction confidence (blank for hand-entered commitments), status control, nudge history, and task-tracker sync history (both directions).
- **Integrations page**:
  - *Nudges* — per-user Slack / Teams / email webhook or API key, cadence (day-before / on-due / daily-once-overdue). "Send test nudge" logs an entry; nothing is sent. Each channel has "how to get these details" steps.
  - *Task tracker* — connect Jira or Google Sheets (or neither), with setup steps for each. Outbound "Sync to tracker" logs what *would* be sent and mints an `external_ref` (e.g. a fake Jira issue key). See two-way sync below.
  - *Transcript source* — configure which app (Zoom / Google Meet / Microsoft Teams / Otter.ai) transcripts would come from, with setup steps for each. "Check for new transcripts" logs what would happen — there's no parser yet to act on a fetched transcript, so this is illustrative until that's wired in. Not configuring one is fine; upload transcripts by hand instead.
- **Two-way tracker sync**: outbound push is mocked (logged only), but `POST /api/webhooks/tracker-update` is a *real, functional* inbound endpoint — point a Jira Automation rule or a Google Apps Script trigger at it and a status change made there updates the matching commitment here. No outbound call is needed to receive a webhook, so this half is safe to make real even while push stays mocked. A "Simulate update from tracker" button in the commitment modal exercises the same path without needing a live external caller.
- **Light/dark theme** toggle, persisted per browser, no flash of the wrong theme on load.
- **Demo data**: a "Load demo meeting" button seeds two sample meetings with realistic commitments so the app isn't empty on first look.

## Tech stack

Plain Python (Flask) + SQLite + static HTML/CSS/vanilla JS. No build step, no frontend framework, no cloud account required.

- `app.py` — Flask app and all API routes
- `schema.sql` — SQLite schema
- `static/` — the three pages (`index.html`, `dashboard.html`, `settings.html`), shared `app.js`, and `style.css`

## Running it

```bash
pip install flask
python app.py
```

Open `http://localhost:5000`, enter any email address, and click **Load demo meeting** to populate sample data (or **Upload transcript** to add your own).

## API overview

All routes except `/`, `/dashboard`, `/settings`, `/api/identify`, and the webhook below require an active session (set via `POST /api/identify`).

| Route | Method | Purpose |
|---|---|---|
| `/api/identify` | POST | Upsert a user by email, start a session |
| `/api/me` / `/api/logout` | GET / POST | Session check / end |
| `/api/meetings` | GET / POST | List meetings / manually create one (transcript upload) |
| `/api/meetings/<id>` | GET | Meeting detail, incl. its commitments |
| `/api/meetings/seed-demo` | POST | Insert demo meetings + commitments |
| `/api/commitments` | GET / POST | List (filter by `status`, `owner`) / manually add one to a meeting |
| `/api/commitments/<id>` | GET / PATCH | Detail (incl. nudge/tracker history) / update status |
| `/api/commitments/<id>/send-test-nudge` | POST | Mock nudge — logs only |
| `/api/commitments/<id>/sync-tracker` | POST | Mock outbound tracker sync — logs only, mints `external_ref` |
| `/api/commitments/<id>/simulate-inbound-update` | POST | UI test button for the inbound sync path (session-authed) |
| `/api/webhooks/tracker-update` | POST | **Real** inbound endpoint — a Jira/Sheets automation posts here |
| `/api/nudge-settings` | GET / POST | List / upsert a nudge channel |
| `/api/tracker-settings` | GET / POST | Get / upsert the user's task tracker config |
| `/api/transcript-source-settings` | GET / POST | Get / upsert which app transcripts come from |
| `/api/transcript-source/check-now` | POST | Mock "check for new transcripts" — logs only |
| `/api/transcript-source/log` | GET | Recent transcript-source check log |

## Known limitations (by design, for now)

- No real authentication — anyone who knows a user's email can "sign in" as them. Fine for a local prototype, not for anything shared.
- Nudges, outbound tracker sync, and transcript-source checks are mocked — see above. Only the inbound tracker webhook and manual transcript/commitment entry are real.
- The inbound webhook isn't protected by anything beyond knowing a user's email + a commitment's `external_ref` — acceptable for a prototype with no public deployment yet, not for a real one (would want a shared secret or signature check).
- No transcript parser is wired in yet (in development separately); commitments on an uploaded transcript must be added by hand for now.
- No connector (ChatGPT/Claude/etc.) is wired up yet; the app only exposes a browser UI + session-cookie API today.
- SQLite + Flask's dev server — not meant to run in production as-is.

## What's next

See the **Roadmap** section of [prd.md](prd.md) for the planned direction (parser integration, connector, hosting decision).
