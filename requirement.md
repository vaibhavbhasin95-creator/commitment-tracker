# Requirements — Commitment Tracker

Companion to [prd.md](prd.md) (the why) — this is the concrete, testable *what*. See [readme.md](readme.md) for how to run it.

## Functional requirements

### Identity
- FR1. A visitor can identify themselves with just an email address — no password, no verification, no OAuth.
- FR2. Identifying with an email that hasn't been seen before creates a user; identifying with a known email reuses it.
- FR3. All commitment/meeting/settings data is scoped to the identifying email; a session persists via cookie until logout.
- FR4. Visiting a protected page (dashboard, settings) without an active session redirects to the landing page's identify form.

### Meetings & commitments
- FR5. A user can load demo data (two sample meetings, five sample commitments) via a single action, repeatable.
- FR6. Each commitment has: task text, owner name (+ optional owner email), due date, status (`pending` / `in_progress` / `done`), a source quote, and an extraction-confidence score.
- FR7. A commitment is flagged **overdue** when its due date has passed and its status is not `done` — computed at read time, not a stored status value.
- FR8. Commitments can be filtered by status and by owner.
- FR9. A user can change a commitment's status from the dashboard; the change is immediate (no confirmation step).

### Dashboard chart
- FR10. The dashboard shows counts of Pending / In Progress / Done across all of the user's commitments (unaffected by active filters).
- FR11. The counts are also shown as a proportional bar, updating whenever a status changes or new data is seeded.

### Nudges (mocked)
- FR12. A user can save one or more nudge channels (Slack, Teams, email), each with a webhook URL / API key, a cadence, and an enabled flag.
- FR13. From a commitment's detail view, "Send test nudge" writes an entry to that commitment's nudge history and shows a confirmation — **no outbound HTTP request is made**.
- FR14. Nudge history is visible per commitment, most recent first.

### Task tracker (mocked)
- FR15. A user can configure at most one external tracker: none, Jira (base URL, project key, account email, API token), or Google Sheets (spreadsheet ID, API key/service-account credential).
- FR16. Saving a tracker other than "none" requires all of that provider's fields; saving "none" requires nothing.
- FR17. A commitment's "Sync to tracker" action is disabled, with an explanatory note, when no tracker is configured or enabled.
- FR18. When enabled, "Sync to tracker" writes a log entry describing what *would* be sent (e.g. which Jira project, which spreadsheet) — **no outbound HTTP request is made**.
- FR19. Tracker sync history is visible per commitment, most recent first.
- FR20. Commitments function identically whether or not a tracker is configured — the tracker is additive, never required.

### Theme
- FR21. A user can toggle light/dark theme from any page; the choice persists across page loads and sessions in that browser (`localStorage`).
- FR22. The correct theme is applied before first paint — no flash of the wrong theme.

## Non-functional requirements

- NFR1. **No real external calls.** Nudge and tracker-sync features must not, under any configuration, make an outbound network request to a third-party service. This is a hard constraint, not a default — it protects against accidental misuse of credentials entered into a prototype.
- NFR2. **No paid tier.** No UI copy, gating, or backend logic may imply a subscription, trial, or paywall.
- NFR3. **Zero external dependencies to run.** `pip install flask` and `python app.py` must be sufficient — no cloud account, no API keys, no build step.
- NFR4. **Platform-neutral.** No code or copy should assume a specific hosting provider or a specific AI-assistant platform (ChatGPT vs. Claude vs. other) until that decision is made.
- NFR5. Pages must render legibly in both light and dark themes, meeting basic contrast expectations (validated for the status-chart palette specifically).
- NFR6. The app is single-instance/local-scale — no requirement for concurrent-user performance, since it isn't deployed anywhere yet.

## Data model requirements

- Schema must stay close to what a real transcript-parsing connector would plausibly emit (task text, owner, due date, source quote, confidence), so a future ingestion endpoint is additive rather than a rebuild.
- Tracker and nudge configuration must be stored per-user (per identifying email), not global, since different users may plug into different tools with different credentials.

## Explicit constraints / assumptions

- Single identifying email = single user; no team/workspace concept yet.
- SQLite file storage; not designed for concurrent write load.
- Flask's built-in dev server; not a production WSGI deployment.
- No automated test suite yet — verification has been manual + scripted browser checks during development.
