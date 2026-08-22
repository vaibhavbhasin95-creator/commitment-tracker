# Commitment Tracker

A local prototype dashboard that turns meeting commitments ("I'll get that done by Friday") into tracked, nudge-able tasks. Built to validate the UX and product concept before committing to a hosting platform or wiring up real integrations.

This app is the **tracking half** of a larger product idea: a transcript-parsing "connector" (built separately) extracts commitments from a meeting, and this dashboard is where a user goes to track, follow up on, and optionally sync those commitments to wherever their team already works.

## Status

Prototype. Free to use, no account system beyond an email address, no payment anywhere. Two integrations — nudges (Slack/Teams/email) and external task-tracker sync (Jira/Google Sheets) — are **simulated**: the settings UI and data model are real, but no outbound network request is ever made. See [prd.md](prd.md) and [requirement.md](requirement.md) for the reasoning.

## Features

- **Landing page** with a sample "extracted from your meeting" card and an email-only "Get started" form (no password, no OAuth).
- **Dashboard**: meetings list, a filterable commitments table (status / owner), overdue flagging, and a compact status-breakdown chart (Pending / In Progress / Done).
- **Commitment detail modal**: source quote, owner, due date, extraction confidence, status control, nudge history, and task-tracker sync history.
- **Integrations page**:
  - *Nudges* — per-user Slack / Teams / email webhook or API key, cadence (day-before / on-due / daily-once-overdue). "Send test nudge" logs an entry; nothing is sent.
  - *Task tracker* — connect Jira or Google Sheets (or neither). "Sync to tracker" logs what *would* be sent; nothing is sent.
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

Open `http://localhost:5000`, enter any email address, and click **Load demo meeting** to populate sample data.

## API overview

All routes except `/`, `/dashboard`, `/settings`, and `/api/identify` require an active session (set via `POST /api/identify`).

| Route | Method | Purpose |
|---|---|---|
| `/api/identify` | POST | Upsert a user by email, start a session |
| `/api/me` / `/api/logout` | GET / POST | Session check / end |
| `/api/meetings` | GET | List the current user's meetings |
| `/api/meetings/seed-demo` | POST | Insert demo meetings + commitments |
| `/api/commitments` | GET | List commitments (filter by `status`, `owner`) |
| `/api/commitments/<id>` | GET / PATCH | Detail (incl. nudge/tracker history) / update status |
| `/api/commitments/<id>/send-test-nudge` | POST | Mock nudge — logs only |
| `/api/commitments/<id>/sync-tracker` | POST | Mock tracker sync — logs only |
| `/api/nudge-settings` | GET / POST | List / upsert a nudge channel |
| `/api/tracker-settings` | GET / POST | Get / upsert the user's task tracker config |

## Known limitations (by design, for now)

- No real authentication — anyone who knows a user's email can "sign in" as them. Fine for a local prototype, not for anything shared.
- Nudges and tracker sync are mocked — see above.
- No connector (ChatGPT/Claude/etc.) is wired up yet; the app only exposes a browser UI + session-cookie API today.
- SQLite + Flask's dev server — not meant to run in production as-is.

## What's next

See the **Roadmap** section of [prd.md](prd.md) for the planned direction (connector integration, real tracker API calls, hosting decision).
