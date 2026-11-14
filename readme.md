# Commitment Tracker

A local web app that turns meeting commitments ("I'll get that done by Friday") into tracked, nudge-able tasks: a transcript comes in, commitments are extracted, each is matched to a person, tracked here (and optionally in a Google Sheet), and owners get reminders.

**Version:** see `APP_VERSION` in [src/app.py](src/app.py) (also shown in the sidebar and at `GET /api/version`). Every change is recorded in [CHANGELOG.md](CHANGELOG.md).

## Status

Local prototype, free to use. **The up-to-date picture of what is real, what is simulated, and what isn't built is the "Current status" section of [prd.md](prd.md)** (PDF: `docs/Commitment_Tracker_PRD_v<version>.pdf`).

Real: sign-in (password or SSO), transcript upload (text, `.docx`, `.pdf`) and Fathom capture, commitment extraction (built-in model or the customer's own AI), asking questions about your meetings with cited answers ([src/rag/README.md](src/rag/README.md)), background jobs, two-way Google Sheets sync, people directory with designations, nudges by email (from your own draft, optionally AI-tailored) / Slack / Teams / custom webhook, signed webhooks, encrypted credentials, live dashboard updates. Still simulated: Jira and custom-tracker outbound sync, and "check now" pulls from Zoom / Google Meet / Teams / Otter.

## Folder layout

All code is in **`src/`**; everything else in the project root is data, docs or generated output.

```
commitment-tracker/
├── src/                      ← all code
│   ├── app.py                  Flask app: routes and glue
│   ├── auth.py                 password + SSO sign-in, sessions, CSRF guard
│   ├── manage_users.py         command line: list accounts, set a password
│   ├── jobs.py                 background job queue + worker
│   ├── webhook_security.py     webhook signatures, replay protection, rate limits
│   ├── document_text.py        .docx / .pdf / .txt → text
│   ├── transcript_parser.py    built-in extraction (ML classifier + rules)
│   ├── ai_extraction.py        optional AI extraction engine (trial feature)
│   ├── ai_writer.py            email-nudge drafts tailored by AI to designation
│   ├── rag/                    ask questions about your meetings: search + cited answers (see rag/README.md)
│   ├── notifications.py        email / Slack / Teams / custom senders
│   ├── people_directory.py     people CSV + owner matching
│   ├── google_sheets.py        Sheets API; google_oauth_setup.py: one-time login
│   ├── secret_store.py         encryption of saved credentials
│   ├── live_events.py          live updates (Server-Sent Events)
│   ├── schema.sql              database schema
│   ├── static/                 pages, app.js, style.css
│   ├── tests/                  test suites (python src/tests/run_all.py)
│   ├── ml/                     training / retraining scripts
│   └── docs_tools/             builds the PRD PDF, diagrams, technical guide
├── commitment_tracker.db     database            ┐
├── secret.key                credential key      │ runtime data — git-ignored,
├── flask_secret.key          session key         │ back up secret.key
├── auth_config.json          SSO settings (opt.) │
├── client_secret_*.json, google_token.json       ┘
├── ml/                       trained models
├── docs/                     PRD, diagrams, technical guide
└── training_data/, *.csv, transcripts            ML data
```

## Running it

From the project root:

```bash
pip install -r requirements.txt
python -m spacy download en_core_web_sm
python src/ml/train_action_detector.py   # one-time: trains ml/action_detector.keras
python src/app.py                        # http://localhost:5000
```

Set `CT_DEBUG=1` for Flask's auto-reloading debug server (localhost only — its debugger runs code from the browser).

**Signing in.** Create an account on the sign-in page (email + password, at least 10 characters). An account created before 0.9.0 has no password; set one from this computer:

```bash
python src/manage_users.py set-password you@example.com
python src/manage_users.py list
```

**Single sign-on (optional).** Copy `auth_config.example.json` to `auth_config.json` and fill in a provider (Google, Microsoft, Okta, Auth0, Keycloak — any OpenID Connect provider). Each provider needs an OAuth app whose redirect URI is `<app URL>/auth/sso/<id>/callback`, e.g. `http://localhost:5000/auth/sso/google/callback`. A "Continue with …" button then appears on the sign-in page. An SSO sign-in with a verified email links to the existing account with that email. Set `"allow_signup": false` to stop new accounts from being created.

**Behind ngrok / HTTPS**, set `CT_COOKIE_SECURE=1` (Secure cookies) and `CT_PUBLIC_URL=https://your-url` (SSO redirect address).

The first extraction after a start is slow (the ML libraries load — several minutes on a slow machine); it runs in the background, so uploads return immediately. Skipping model training is fine: the app runs, uploads just get no automatic commitments until the model exists.

### Environment variables

| Variable | Default | Purpose |
|---|---|---|
| `CT_DEBUG` | off | `1` = Flask debug server with auto-reload |
| `PORT` | 5000 | Port |
| `CT_COOKIE_SECURE` | off | `1` when served over HTTPS |
| `CT_PUBLIC_URL` | request host | Public base URL for SSO redirects |
| `CT_ALLOW_SIGNUP` | on | `0` = no new accounts (also `allow_signup` in auth_config.json) |
| `CT_SSO_PROVIDERS`, `CT_SSO_<ID>_CLIENT_ID`, `…_CLIENT_SECRET`, `…_ISSUER` | — | SSO without a config file |
| `COMMITMENT_TRACKER_SECRET_KEY` | `secret.key` | Credential encryption key |
| `COMMITMENT_TRACKER_FLASK_SECRET` | `flask_secret.key` | Session signing key |
| `CT_ALLOW_UNSIGNED_TRACKER_WEBHOOK` | off | `1` = keep the retired unsigned tracker webhook working temporarily |
| `CT_JOBS_INLINE` | off | `1` = run jobs inside the request (tests) |
| `NUDGE_CHECK_INTERVAL_SECONDS` | 900 | Automatic nudge check interval |
| `AI_EXTRACTION_ENABLED` | on | `0` = turn the optional AI extraction feature off |
| `RAG_ENABLED` | on | `0` = turn the optional "Ask your meetings" feature off (no page, routes or indexing) |
| `ANTHROPIC_API_KEY` | — | AI-suggested setup steps for custom apps |

### Ask your meetings

Answers a question from your saved meetings, with the source of every claim. Full details in [src/rag/README.md](src/rag/README.md).

1. On the **AI extraction** page, save an AI engine and confirm it (a local model such as GPT4All or Ollama works and keeps everything on your machine; the first question after a start can take a minute while the models load). Extraction can stay on Built-in.
2. Open **Ask your meetings** in the sidebar and type a question.
3. Meetings are indexed automatically in the background, including existing ones, within about a minute of the app starting. `python src/rag/backfill.py` indexes everything right away.

The first use downloads a small embedding model (about 90 MB) from Hugging Face; behind a firewall that blocks it, download it once and point `SENTENCE_TRANSFORMERS_HOME` at the folder.

### Tests

```bash
python src/tests/run_all.py          # every suite; temporary databases and fake services only
python src/tests/run_all.py rag      # just the "Ask your meetings" suites
```

## Webhooks

Inbound webhooks must be **signed** with the account's signing secret (Integrations → Webhook security): headers `X-CT-Timestamp` (unix seconds) and `X-CT-Signature: v1=<hex HMAC-SHA256 of "<timestamp>.<body>">`, optionally `X-CT-Delivery` (unique id). Requests older than 5 minutes, replays and more than 60 a minute per URL are refused. The Integrations page has a ready-made Google Apps Script and a Python example.

| URL | Used by |
|---|---|
| `POST /api/webhooks/tracker-update/<token>` | Google Sheet / tracker edits → app (task, owner, owner_email, due_date, status; most recent edit wins) |
| `POST /api/webhooks/transcript-capture/<token>` | Any tool pushing a transcript |
| `POST /api/webhooks/fathom/<token>` | Fathom (signed with Fathom's own secret) |

## API overview

Everything except the sign-in routes and webhooks needs a signed-in session; every POST/PATCH/DELETE needs the header `X-CT-Request: 1`.

| Route | Method | Purpose |
|---|---|---|
| `/api/auth/config` | GET | Sign-up allowed, SSO buttons |
| `/api/auth/signup` · `/api/auth/login` · `/api/logout` | POST | Create account · sign in · sign out |
| `/api/auth/password` · `/api/auth/logout-everywhere` | POST | Change password · end all sessions |
| `/auth/sso/<id>/start` → `/callback` | GET | SSO sign-in |
| `/api/me` | GET | Signed-in account |
| `/api/meetings` | GET / POST | List (with task count + extraction state) / create — JSON or multipart with a `.txt`/`.docx`/`.pdf` `file` |
| `/api/meetings/<id>` | GET / DELETE | Detail with commitments / delete with its tasks |
| `/api/meetings/clear` | POST | Delete all meetings (`{"confirm": true}`) |
| `/api/meetings/<id>/extract-commitments` | POST | Re-run extraction (queued as a job) |
| `/api/commitments` | GET / POST | List (`meeting_id`, `status`, `owner` filters) / add one |
| `/api/commitments/clear` | POST | Delete tasks — all, or one meeting's (`{"confirm": true, "meeting_id": …}`) |
| `/api/commitments/<id>` | GET / PATCH | Detail / edit task_text, owner_name, owner_email, due_date, status (synced to the sheet) |
| `/api/commitments/<id>/send-test-nudge` · `/sync-tracker` | POST | Send a nudge now · sync to the tracker |
| `/api/jobs` · `/api/jobs/<id>/retry` · `/cancel` | GET / POST | Background jobs |
| `/api/people` · `/api/people/upload` · `/api/people/<id>` | … | People directory (with designation) |
| `/api/nudge-settings`, `/api/nudge-preferences`, `/api/nudges/run-now` | … | Nudge channels (email draft included), automatic nudges |
| `/api/email-draft/defaults` · `/api/email-draft/preview` | GET / POST | Draft placeholders · preview an (AI-tailored) email |
| `/api/tracker-settings`, `/api/tracker/sync-all` | … | Task tracker |
| `/api/transcript-source-settings`, `/api/transcript-source/…` | … | Transcript source |
| `/api/webhook-security` · `/reveal` · `/rotate` | GET / POST | Webhook URLs, signing secret, recent deliveries |
| `/api/events` | GET | Live updates stream |
| `/ask` | GET | The "Ask your meetings" page |
| `/api/rag/ask` | POST | `{"question": "..."}` → answer with citations (409 `consent_required` for a cloud engine's first use) |
| `/api/rag/consent` · `/api/rag/reindex` | POST | One-time consent for a cloud engine · rebuild one meeting's search index |
| `/api/rag/status` | GET | Whether the feature is on |

## Known limitations

The full list is in [prd.md](prd.md) under "Not built". The most important:

- One app process: background jobs and live updates don't span several server processes.
- No email verification or password reset by email yet — reset with `manage_users.py`.
- Scanned PDFs (no text layer) can't be read; there's no OCR.
- Extraction quality on real meetings is limited until the model is retrained on real business-meeting transcripts.
- "Ask your meetings" answers are only as good as the AI engine behind them, and its relevance thresholds are starting guesses that haven't yet been measured on a set of real questions (see [src/rag/README.md](src/rag/README.md) for the limits).
- Inbound webhooks need a public URL (ngrok today). SQLite + Flask's server aren't meant for production as-is.
