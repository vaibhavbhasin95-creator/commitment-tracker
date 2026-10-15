-- Meeting Commitment Tracker — local prototype schema

CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    email TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    password_hash TEXT,                          -- NULL for SSO-only accounts and accounts from before 0.9.0
    display_name TEXT,
    session_version INTEGER NOT NULL DEFAULT 1,  -- bumped on password change / sign out everywhere
    last_login_at TEXT
);

CREATE TABLE IF NOT EXISTS meetings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_email TEXT NOT NULL,
    title TEXT NOT NULL,
    meeting_date TEXT NOT NULL,
    created_at TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'generated',   -- 'upload' | 'generated' | 'live'
    transcript_excerpt TEXT,
    transcript_provider TEXT                    -- which app delivered it live (e.g. 'zoom'), NULL unless source='live'
);

CREATE TABLE IF NOT EXISTS commitments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    meeting_id INTEGER NOT NULL REFERENCES meetings(id),
    task_text TEXT NOT NULL,
    owner_name TEXT NOT NULL,
    owner_email TEXT,
    due_date TEXT,
    status TEXT NOT NULL DEFAULT 'pending',      -- 'pending' | 'in_progress' | 'done'
    confidence_score REAL,                       -- NULL for manually-entered commitments
    source_quote TEXT,
    external_ref TEXT,                           -- id/key in the external tracker, once synced (e.g. a Jira issue key)
    created_at TEXT NOT NULL,
    owner_match TEXT,                            -- how owner_email was set: 'auto' | 'manual' | 'ambiguous' | 'none'; NULL = never looked up
    updated_at TEXT                              -- last change in the app or from the tracker; NULL = never changed since created_at
);

-- The account's people directory (uploaded as CSV): used to find each task
-- owner's email so nudges can reach them. Transcripts only carry names.
CREATE TABLE IF NOT EXISTS people (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    owner_email TEXT NOT NULL,                   -- the app account this directory belongs to
    first_name TEXT NOT NULL DEFAULT '',
    last_name TEXT NOT NULL DEFAULT '',
    email TEXT NOT NULL,
    username TEXT NOT NULL DEFAULT '',           -- corporate user name, e.g. jsmith or DOMAIN\jsmith
    designation TEXT NOT NULL DEFAULT '',        -- job title, e.g. 'VP Finance' — sets the tone of AI-tailored email nudges
    created_at TEXT NOT NULL,
    UNIQUE(owner_email, email)
);

CREATE TABLE IF NOT EXISTS nudge_settings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_email TEXT NOT NULL,
    channel TEXT NOT NULL,                       -- 'slack' | 'teams' | 'email' | 'custom'
    custom_name TEXT,                            -- user-supplied app name, only set when channel = 'custom'
    webhook_or_key TEXT NOT NULL,
    cadence TEXT NOT NULL DEFAULT 'day_before',   -- 'day_before' | 'on_due' | 'daily_after_overdue'
    enabled INTEGER NOT NULL DEFAULT 1,
    config_json TEXT NOT NULL DEFAULT '{}',      -- extra channel settings, e.g. SMTP server details for email
    UNIQUE(user_email, channel)
);

CREATE TABLE IF NOT EXISTS nudge_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    commitment_id INTEGER NOT NULL REFERENCES commitments(id),
    triggered_at TEXT NOT NULL,
    channel TEXT NOT NULL,
    status TEXT NOT NULL,                        -- 'sent' | 'failed' | 'skipped' | 'mock_sent' (before 0.6.0)
    reason TEXT,                                 -- 'test' | 'day_before' | 'on_due' | 'overdue'
    detail TEXT
);

-- Per-account switch for sending nudges automatically on each channel's cadence.
CREATE TABLE IF NOT EXISTS nudge_preferences (
    user_email TEXT PRIMARY KEY,
    auto_enabled INTEGER NOT NULL DEFAULT 0
);

-- A user's external task tracker, e.g. their team already lives in Jira or a
-- shared Google Sheet. One tracker per user (unlike nudge_settings, which
-- allows several channels) — you follow up on a commitment in one place.
CREATE TABLE IF NOT EXISTS tracker_settings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_email TEXT NOT NULL UNIQUE,
    provider TEXT NOT NULL DEFAULT 'none',        -- 'none' | 'jira' | 'google_sheets' | 'custom'
    custom_name TEXT,                             -- user-supplied app name, only set when provider = 'custom'
    config_json TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS tracker_sync_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    commitment_id INTEGER NOT NULL REFERENCES commitments(id),
    provider TEXT NOT NULL,
    direction TEXT NOT NULL DEFAULT 'outbound',    -- 'outbound' (we pushed) | 'inbound' (tracker pushed to us)
    triggered_at TEXT NOT NULL,
    status TEXT NOT NULL,                          -- 'mock_synced' | 'synced' | 'received' | 'skipped'
    detail TEXT
);

-- Where the transcript itself is expected to come from. Separate from
-- tracker_settings (that's where *tasks* go out to; this is where the
-- *transcript* comes in from). "Check now" is still mocked, but Zoom /
-- Google Meet / Teams also support a real, live push via a per-user
-- webhook_token (see /api/webhooks/... routes) once connected.
CREATE TABLE IF NOT EXISTS transcript_source_settings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_email TEXT NOT NULL UNIQUE,
    provider TEXT NOT NULL DEFAULT 'none',         -- 'none' | 'zoom' | 'google_meet' | 'teams' | 'otter' | 'custom'
    custom_name TEXT,                              -- user-supplied app name, only set when provider = 'custom'
    config_json TEXT NOT NULL DEFAULT '{}',
    enabled INTEGER NOT NULL DEFAULT 1,
    webhook_token TEXT UNIQUE                      -- identifies this user in inbound capture webhook URLs; generated on first save
);

CREATE TABLE IF NOT EXISTS transcript_sync_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_email TEXT NOT NULL,
    provider TEXT NOT NULL,
    direction TEXT NOT NULL DEFAULT 'outbound',    -- 'outbound' (we checked/pulled) | 'inbound' (a provider pushed a transcript to us)
    triggered_at TEXT NOT NULL,
    status TEXT NOT NULL,                          -- 'mock_checked' | 'received'
    detail TEXT,
    meeting_id INTEGER REFERENCES meetings(id)     -- set for inbound entries that created a meeting; NULL for a mock "check now"
);
