# Product Requirements Document — Commitment Tracker

## Problem

Meetings generate spoken commitments ("I'll send that by Friday") that routinely get lost — no one wrote them down, no one owns following up, and there's no single place to see what's pending, in progress, or done across meetings. Existing task trackers (Jira, Sheets, etc.) require someone to manually transcribe the commitment into them, which mostly doesn't happen.

## Product concept

Two cooperating pieces:

1. **Parsing engine** — extracts commitments from a meeting transcript: who owns what, by when, with the source quote. In active development separately (currently a Google Colab notebook), expected to move into this repo within roughly a week; no decision yet on how it will hand transcripts/commitments to this app.
2. **Commitment Engine** (this app) — takes those commitments and makes them trackable and nudge-able, optionally syncing to wherever the user's team already tracks work. Until the parsing engine lands, meetings and commitments are entered here by hand (paste/upload a transcript, then add commitments to it manually).

Original go-to-market framing was a "freemium hook": a free connector running inside an AI assistant (ChatGPT/Claude) does the parsing, then hands the user a link into this dashboard to track and get nudged about what it found. That framing is **on hold** — the parsing side's platform isn't decided yet, and the product itself has been explicitly scoped to stay **free for everyone**, with no subscription or paywall language anywhere. The dashboard is being validated as its own standalone piece first.

## Target user

Anyone who leaves meetings with verbal commitments that need follow-up — individually or in a small team — regardless of which meeting platform (Google Meet, Teams, Zoom, Slack huddle) or which task tracker (or none) they already use. Explicitly not gated behind an enterprise tier of any of those tools.

## Goals

- Validate the core loop — see a commitment, know its status, get nudged, mark it done — is clear and useful before investing in real integrations or a hosting decision.
- Prove the data model is compatible with a future real parsing engine (commitment schema close to what an extractor would emit) and a future real tracker sync (Jira/Sheets config shape already realistic).
- Keep everything reversible: no cloud account, no vendor lock-in, no real credentials required to try it.

## Non-goals (for this phase)

- Real authentication (OAuth/SSO) — explicitly deferred; email-only identification is enough to validate the UX.
- Real outbound Slack/Teams/email delivery, real outbound Jira/Google Sheets writes, or real Zoom/Meet/Teams/Otter transcript fetching — all mocked; wiring them up is separate future work once providers/OAuth approaches are chosen. (The inbound half of tracker sync is the one deliberate exception — see below.)
- Any payment or tiering system.
- A specific hosting platform — deliberately not decided yet.

## Scope

### In scope (built)
- Email-only identify flow, session-based.
- Meetings + commitments data model, demo seed data.
- Manual transcript upload (paste or `.txt` file) and manual commitment entry — the real, working stand-in for the parsing engine until it's integrated.
- Dashboard: status chart, filterable table, overdue flagging, commitment detail, meeting detail.
- Nudge settings (mocked) — Slack, Teams, email; configurable cadence; per-provider setup steps.
- Task tracker settings — Jira, Google Sheets, or none; commitments work identically either way; per-provider setup steps. Outbound push is mocked; **inbound sync is real** — a working webhook endpoint lets a status change made in Jira/Sheets update the commitment here, so the tracker isn't just a one-way announcement.
- Transcript source settings (mocked) — which app (Zoom/Meet/Teams/Otter.ai) transcripts would come from, with per-provider setup steps; independent of whether a real fetch is ever wired up, since manual upload always works.
- Light/dark theme.

### Out of scope (not built yet)
- The transcript-parsing engine itself (in progress elsewhere) and any automatic hand-off from it into this app.
- Real outbound nudge delivery, real outbound tracker writes, or real transcript fetching from any provider.
- Multi-user/team workspaces (today every meeting/commitment belongs to a single identifying email).
- Mobile app / native clients.

## Key user stories

1. As someone who just left a meeting, I want to see the commitments that came out of it in one place, so nothing falls through.
2. As a commitment owner, I want to mark my task pending / in progress / done, and see whether it's overdue.
3. As a user whose team already lives in Jira or a shared Sheet, I want to connect that instead of learning a new tool, so this app doesn't compete with what we already use.
4. As a returning user, I want a quick visual sense of what's outstanding vs. done without reading every row.
5. As a first-time visitor, I want to try the app without creating an account or handing over a password.
6. As someone without a transcript-parsing setup yet, I want to paste in a transcript and note down the commitments myself, so the app is useful before the automated pipeline exists.
7. As a Jira/Sheets user, I want a status change I make *there* to show up here too, not just changes I make in this app — otherwise I have to keep both places in sync by hand, which defeats the point.
8. As someone picking a settings option I don't recognize (a Jira API token, a Zoom scope), I want the app to tell me where to go get it, so I'm not left to search for it myself.

## Open questions / decisions still needed

- **Transcript hand-off**: the parsing engine (separate Colab project, ~1 week out) has no defined way to get its output into this app yet. The schema and a manual-entry path are ready to receive it; the actual hand-off mechanism (direct DB write, an API call, a file drop) is undecided.
- **Connector platform**: ChatGPT (Custom GPT Actions), Claude (MCP server), both, or neither for now? Blocks building an AI-assistant-driven hand-off specifically.
- **Hosting**: where does this get deployed once it's more than a local prototype? Needed before any connector — or any real external webhook (Jira/Sheets calling *us*) — can reach it; everything inbound only works against `localhost` today.
- **Auth for machine callers**: the current email-cookie session won't work for a connector calling the API directly, and the inbound tracker webhook currently trusts `email` + `external_ref` alone — both need a real credential (API key, shared secret) before this is anything but a local prototype.
- **Real outbound tracker integration**: Jira uses API tokens (straightforward); Google Sheets realistically needs OAuth or a service account — which to build first depends on user demand. Inbound is already real for both, so the shape has already been proven.

## Roadmap (proposed order)

1. ~~Local prototype: dashboard, mock nudges, mock tracker sync, chart~~ — done.
2. ~~Manual transcript upload + commitment entry, transcript source settings, two-way tracker sync (real inbound webhook + simulate button), per-provider setup steps~~ — done.
3. Land the real parsing engine and decide how it hands transcripts/commitments to this app.
4. Decide hosting + add API-key/shared-secret auth for machine callers (connectors and the inbound webhook alike).
5. Build one connector (MCP or GPT Action) that reads/writes commitments via the API.
6. Wire up one real *outbound* integration end-to-end (likely Jira first — token auth, no OAuth dance) now that inbound already works, as proof the mocked shape holds up against a real API in both directions.
7. Revisit the freemium funnel framing once the connector exists to test it against.
