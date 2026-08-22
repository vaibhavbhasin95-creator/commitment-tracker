# Product Requirements Document — Commitment Tracker

## Problem

Meetings generate spoken commitments ("I'll send that by Friday") that routinely get lost — no one wrote them down, no one owns following up, and there's no single place to see what's pending, in progress, or done across meetings. Existing task trackers (Jira, Sheets, etc.) require someone to manually transcribe the commitment into them, which mostly doesn't happen.

## Product concept

Two cooperating pieces:

1. **Parsing engine** (built separately, platform undecided) — extracts commitments from a meeting transcript: who owns what, by when, with the source quote.
2. **Commitment Engine** (this app) — takes those commitments and makes them trackable and nudge-able, optionally syncing to wherever the user's team already tracks work.

Original go-to-market framing was a "freemium hook": a free connector running inside an AI assistant (ChatGPT/Claude) does the parsing, then hands the user a link into this dashboard to track and get nudged about what it found. That framing is **on hold** — the parsing side's platform isn't decided yet, and the product itself has been explicitly scoped to stay **free for everyone**, with no subscription or paywall language anywhere. The dashboard is being validated as its own standalone piece first.

## Target user

Anyone who leaves meetings with verbal commitments that need follow-up — individually or in a small team — regardless of which meeting platform (Google Meet, Teams, Zoom, Slack huddle) or which task tracker (or none) they already use. Explicitly not gated behind an enterprise tier of any of those tools.

## Goals

- Validate the core loop — see a commitment, know its status, get nudged, mark it done — is clear and useful before investing in real integrations or a hosting decision.
- Prove the data model is compatible with a future real parsing engine (commitment schema close to what an extractor would emit) and a future real tracker sync (Jira/Sheets config shape already realistic).
- Keep everything reversible: no cloud account, no vendor lock-in, no real credentials required to try it.

## Non-goals (for this phase)

- Real authentication (OAuth/SSO) — explicitly deferred; email-only identification is enough to validate the UX.
- Real Slack/Teams/email delivery, or real Jira/Google Sheets API calls — both are mocked; wiring them up is separate future work once a provider/OAuth approach is chosen.
- Any payment or tiering system.
- A specific hosting platform — deliberately not decided yet.

## Scope

### In scope (built)
- Email-only identify flow, session-based.
- Meetings + commitments data model, demo seed data.
- Dashboard: status chart, filterable table, overdue flagging, commitment detail.
- Nudge settings (mocked) — Slack, Teams, email; configurable cadence.
- Task tracker settings (mocked) — Jira, Google Sheets, or none; commitments work identically either way.
- Light/dark theme.

### Out of scope (not built yet)
- The transcript-parsing connector itself.
- Real outbound nudge delivery or tracker API calls.
- Multi-user/team workspaces (today every meeting/commitment belongs to a single identifying email).
- Mobile app / native clients.

## Key user stories

1. As someone who just left a meeting, I want to see the commitments that came out of it in one place, so nothing falls through.
2. As a commitment owner, I want to mark my task pending / in progress / done, and see whether it's overdue.
3. As a user whose team already lives in Jira or a shared Sheet, I want to connect that instead of learning a new tool, so this app doesn't compete with what we already use.
4. As a returning user, I want a quick visual sense of what's outstanding vs. done without reading every row.
5. As a first-time visitor, I want to try the app without creating an account or handing over a password.

## Open questions / decisions still needed

- **Connector platform**: ChatGPT (Custom GPT Actions), Claude (MCP server), both, or neither for now? Blocks building the actual parsing hand-off.
- **Hosting**: where does this get deployed once it's more than a local prototype? Needed before any connector can call it (both ChatGPT and Claude connectors require a public HTTPS endpoint).
- **Auth for machine callers**: the current email-cookie session won't work for a connector calling the API directly — needs an API-key path.
- **Real tracker integration**: Jira uses API tokens (straightforward); Google Sheets realistically needs OAuth or a service account — which to build first depends on user demand.

## Roadmap (proposed order)

1. ~~Local prototype: dashboard, mock nudges, mock tracker sync, chart~~ — done.
2. Decide hosting + add API-key auth for machine callers.
3. Build one connector (MCP or GPT Action) that reads/writes commitments via the API.
4. Wire up one real integration end-to-end (likely Jira first — token auth, no OAuth dance) as a proof that the mocked shape holds up against a real API.
5. Revisit the freemium funnel framing once the connector exists to test it against.
