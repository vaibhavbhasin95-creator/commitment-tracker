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
- FR6. Each commitment has: task text, owner name (+ optional owner email), due date, status (`pending` / `in_progress` / `done`), a source quote, an extraction-confidence score, and an optional external-tracker reference.
- FR7. A commitment is flagged **overdue** when its due date has passed and its status is not `done` — computed at read time, not a stored status value.
- FR8. Commitments can be filtered by status and by owner.
- FR9. A user can change a commitment's status from the dashboard; the change is immediate (no confirmation step).
- FR9a. A user can create a meeting manually by supplying a title, date, and a transcript (pasted text or an uploaded `.txt` file) — a real write, and (when the extraction model is trained) commitments are auto-extracted from the transcript as part of the same request.
- FR9b. From a meeting's detail view, a user can add a commitment to it by hand (task text, owner name, optional owner email/due date/source quote), and can re-run extraction over the meeting's saved transcript on demand. Manually-entered commitments have no confidence score, distinguishing them from extracted ones.

### Dashboard chart
- FR10. The dashboard shows counts of Pending / In Progress / Done across all of the user's commitments (unaffected by active filters).
- FR11. The counts are also shown as a proportional bar, updating whenever a status changes or new data is seeded.

### Nudges (mocked)
- FR12. A user can save one or more nudge channels (Slack, Teams, email), each with a webhook URL / API key, a cadence, and an enabled flag.
- FR13. From a commitment's detail view, "Send test nudge" writes an entry to that commitment's nudge history and shows a confirmation — **no outbound HTTP request is made**.
- FR14. Nudge history is visible per commitment, most recent first.

### Task tracker (outbound mocked, inbound real)
- FR15. A user can configure at most one external tracker: none, Jira (base URL, project key, account email, API token), or Google Sheets (spreadsheet ID, API key/service-account credential).
- FR16. Saving a tracker other than "none" requires all of that provider's fields; saving "none" requires nothing.
- FR17. A commitment's "Sync to tracker" action is disabled, with an explanatory note, when no tracker is configured or enabled.
- FR18. When enabled, "Sync to tracker" writes a log entry describing what *would* be sent (e.g. which Jira project, which spreadsheet) — **no outbound HTTP request is made**. The first successful sync assigns the commitment a fake `external_ref` (e.g. a generated Jira issue key) if it doesn't already have one.
- FR19. Tracker sync history is visible per commitment, most recent first, and each entry is tagged with its direction (outbound = pushed by us, inbound = received from the tracker).
- FR20. Commitments function identically whether or not a tracker is configured — the tracker is additive, never required.
- FR20a. `POST /api/webhooks/tracker-update` is a real, functional endpoint: given a user's email, a commitment's `external_ref`, and a new status, it updates that commitment's status and logs an inbound sync entry. It performs no outbound call itself, so it doesn't violate the "no outbound calls" constraint (NFR1) — it only receives. It is not gated by the session cookie, since an external caller has no browser session.
- FR20b. A commitment with an `external_ref` shows a "Simulate update from tracker" control (status picker + button) that exercises the same inbound path as the real webhook, so two-way sync is demonstrable without a live external automation calling in. Disabled until the commitment has an `external_ref` (i.e., has been synced outbound at least once).

### Transcript source (mocked)
- FR20c. A user can configure at most one transcript source: none, Zoom (account email, API key), Google Meet (workspace email, API key), Microsoft Teams (tenant ID, API key), or Otter.ai (account email, API key).
- FR20d. Saving a source other than "none" requires all of that provider's fields; saving "none" requires nothing.
- FR20e. "Check for new transcripts" is disabled, with an explanatory note, when no source is configured or enabled; when enabled, it writes a log entry describing what *would* be checked — **no outbound HTTP request is made**. A transcript that arrives through the real live-capture webhook path (rather than this mocked pull) is extracted the same way a manual upload is.
- FR20f. Not configuring a transcript source has no effect on the rest of the app — meetings can always be added manually (see FR9a).

### Setup guidance
- FR20g. Every provider option across Nudges, Task Tracker, and Transcript Source shows an expandable "how to get these details" step list specific to that provider, so a user isn't left to search for where a webhook URL or API key comes from.
- FR20h. Each of Nudges, Task Tracker, and Transcript Source offers a "Custom" option alongside the predefined providers, letting a user name their own app and enter generic credentials (an endpoint/webhook URL and an API key), for tools not on the predefined list.
- FR20i. For a Custom app, the "how to get these details" steps are not hand-written; they are suggested on request via a live AI call, scoped to the category (nudge / tracker / transcript source) and the app name the user typed. If no AI provider is configured, or the call fails, a generic fallback list is shown instead — either way, the app keeps working.
- FR20j. Any setup steps produced by FR20i (AI or fallback) are visibly labeled with a disclaimer that they are AI-suggested, unverified, and may be inaccurate — distinct from the curated, hand-written steps for predefined providers (FR20g), which carry no such disclaimer.

### Theme
- FR21. A user can toggle light/dark theme from any page; the choice persists across page loads and sessions in that browser (`localStorage`).
- FR22. The correct theme is applied before first paint — no flash of the wrong theme.

## Non-functional requirements

- NFR1. **No real outbound calls.** Nudges, outbound tracker sync, and transcript-source checks must not, under any configuration, make an outbound network request to a third-party service. This is a hard constraint, not a default — it protects against accidental misuse of credentials entered into a prototype. The one deliberate exception is the *inbound* tracker webhook (FR20a): receiving a request needs no outbound call or credential of our own, so it's allowed to be real while every push direction stays mocked.
- NFR2. **No paid tier.** No UI copy, gating, or backend logic may imply a subscription, trial, or paywall.
- NFR3. **Zero external dependencies to boot.** `pip install flask` and `python app.py` must be sufficient to start the app and use it — no cloud account, no API keys, no build step. Two features degrade gracefully rather than being required: FR20i's AI-suggested setup steps for a Custom app need an `ANTHROPIC_API_KEY` environment variable (falls back to a generic template without it), and commitment extraction (FR9a) needs `pip install -r requirements.txt` plus a trained model (`ml/train_action_detector.py`) — without either, meetings and commitments can still be entered by hand exactly as before, and `POST /api/meetings` reports why extraction didn't run instead of failing the request.
- NFR4. **Platform-neutral.** No code or copy should assume a specific hosting provider or a specific AI-assistant platform (ChatGPT vs. Claude vs. other) until that decision is made.
- NFR5. Pages must render legibly in both light and dark themes, meeting basic contrast expectations (validated for the status-chart palette specifically).
- NFR6. The app is single-instance/local-scale — no requirement for concurrent-user performance, since it isn't deployed anywhere yet.

## Data model requirements

- Schema must stay close to what a real transcript-parsing engine would plausibly emit (task text, owner, due date, source quote, confidence), so a future ingestion endpoint is additive rather than a rebuild. Manual entry (FR9a/FR9b) already exercises this same schema, so it's proven, not just theoretical.
- Tracker, nudge, and transcript-source configuration must each be stored per-user (per identifying email), not global, since different users may plug into different tools with different credentials.
- A commitment's `external_ref` is the join point between this app and an external tracker; it's assigned by us on first outbound sync (a stand-in for what a real API would return), and matched against on inbound update.

## Explicit constraints / assumptions

- Single identifying email = single user; no team/workspace concept yet.
- SQLite file storage; not designed for concurrent write load.
- Flask's built-in dev server; not a production WSGI deployment.
- No automated test suite yet — verification has been manual + scripted browser checks during development.
- The inbound webhook (FR20a) is reachable only on `localhost` today; it can't be pointed at by a real Jira/Sheets automation until this app is deployed somewhere with a public URL.
